# Sample-based verification: normative specification

`fluxsim verify --sample` compares a run's emits with the profile its
Simulation pins (PROFILE-SPEC.md) on a sample of whole blocks of records
instead of every record. Every estimate says how sure it is: each comes with
its sample size and a 95 % interval, and the interval, not the estimate
alone, decides the check.

Only the comparison with the profile is sampled. The other legs of `verify`
read every record as before, the artifacts' digests and the rows'
conformance among them, so a sample saves the comparison's decoding and
statistics, not the read of the emit.

This release estimates means. The other property kinds are named as not
estimated (§4), and `fluxsim verify` without `--sample` checks them as
before. `fluxsim ship` always reads every record.

## 1. The request

| Option | Holds |
|---|---|
| `--sample FRACTION` | The share of each emit's blocks to read: a decimal in `(0, 1]`, read exactly. |
| `--sample-block N` | Records per block, at least 1; 1000 when not given. |
| `--sample-seed S` | A whole number, 0 when not given, that chooses which blocks are read. |

A sampled verify compares with the pinned profile, so the run's plan must
pin one and `--profile` must hand it over (`FLX-3109` otherwise, as for any
pinned run). An emit checked against a `--committed-slice` is compared with
its slice in full, as before, and with the profile on its sample: its leg
fails when either fails, as without `--sample`.

## 2. The blocks

An emit's records, in the order of its stream, are cut into blocks of `N`
consecutive records; the last block may be shorter. The stream is split into
lines as the full comparison splits it, and every line that is not empty is
a record. The blocks stand in for
partitions until emits have partitions. With `R` records there are
`B = ceil(R / N)` blocks, and `n = min(B, max(2, ceil(F * B)))` of them are
read: never fewer than two, which a variance needs.

The blocks read are the `n` whose SHA-256 of the UTF-8 text
`<runId>/<exposeId>`, a NUL, the seed in decimal, a NUL and the block's
index in decimal, spelled in lowercase hex, sorts first. The same run, emit
and seed always read the same blocks, and every set of `n` blocks is as
likely as any other. Only the chosen blocks' lines are decoded; a line that
is not JSON, or a row whose width is not the header's, refuses the emit
(`FLX-8001`). The emit's record count, and the profile's row count when it
publishes one, are checked against the block's declared `records` as the
full comparison checks them (`FLX-5005`).

## 3. The estimate

Each `mean/1` group is estimated with the one-stage cluster ratio estimator,
the blocks being the clusters. For each chosen block `c`, `y_c` is the sum
of the group's encoded values in it and `m_c` their number, read exactly as
the full comparison reads them: the same null policy, the same range
refusal, the same grid (SEAM-FIDELITY-SPEC §5). A block with no record of
the group adds `(0, 0)`. Then

    R = sum(y) / sum(m)
    v = (1 - n/B) * n/(n - 1) * sum((y_c - R * m_c)^2) / sum(m)^2

and the interval is `R +/- t * sqrt(v)`, where `t` is Student's t at 0.975
for `n - 1` degrees of freedom. `1 - n/B` is the finite-population
correction: when every block is read, `v` is 0 and the estimate is the
group's mean itself.

Every number is exact. `t` is read from the table below, each value the true
one rounded up at the fourth decimal, computed from the closed form of the t
distribution for whole degrees of freedom (Abramowitz and Stegun 26.7.3 and
26.7.4); a degree of freedom between two entries reads the smaller one's
value, which is the larger `t`, and one past 120 reads 120's. `sqrt(v)` is
bounded above at 64 fractional bits with an integer square root. Both widen
the interval and never narrow it.

| Degrees of freedom | t | Degrees of freedom | t |
|---|---|---|---|
| 1 | 12.7063 | 16 | 2.1200 |
| 2 | 4.3027 | 17 | 2.1099 |
| 3 | 3.1825 | 18 | 2.1010 |
| 4 | 2.7765 | 19 | 2.0931 |
| 5 | 2.5706 | 20 | 2.0860 |
| 6 | 2.4470 | 21 | 2.0797 |
| 7 | 2.3647 | 22 | 2.0739 |
| 8 | 2.3061 | 23 | 2.0687 |
| 9 | 2.2622 | 24 | 2.0639 |
| 10 | 2.2282 | 25 | 2.0596 |
| 11 | 2.2010 | 26 | 2.0556 |
| 12 | 2.1789 | 27 | 2.0519 |
| 13 | 2.1604 | 28 | 2.0485 |
| 14 | 2.1448 | 29 | 2.0453 |
| 15 | 2.1315 | 30 | 2.0423 |
| | | 40 | 2.0211 |
| | | 60 | 2.0003 |
| | | 120 | 1.9800 |

A group the profile publishes and no chosen block holds is undecided: the
sample says nothing of it. A group the sample holds and the profile does not
refuses the property (`FLX-5005`), as two sides that group into different
sets do in the full comparison. A group the profile withheld is censored.

The full comparison's floors hold. A group's floor is the block's
`records.min` for a grouped property and 1 for an ungrouped one, and a
profile group below its floor refuses the property (`FLX-5005`). When every
block is read, the run's groups are held to their floor too, and the two
sides must group into the same set. On a partial sample, a group whose
chosen blocks hold fewer records than its floor is undecided: the blocks not
read may hold the rest.

## 4. The decision

A group's band is the profile's mean, `Sx / k` in grid units, plus or minus
the property's compiled margin: the margin in grid steps for an `absolute`
tolerance, and the margin times the profile mean's magnitude for a
`relative` one (a zero profile sum refuses the property, `FLX-5005`). The
group is

- `within` when its interval lies inside the band, edges included;
- `outside` when its interval lies wholly below or wholly above the band;
- `undecided` when its interval crosses an edge of the band. Only a larger
  sample, or the whole emit, can settle it.

A property is `outside` when any of its groups is, else `undecided` when
any is, else `within`; it is `censored` when the profile withheld every
group. The properties of another kind are not estimated: they are named, and
nothing is compared for them.

An emit's sampled check is `failed` when a property is `outside` or
`undecided` or a fail-closed condition is met, `passed` when at least one
property is `within` and none failed, and `not checked` when nothing was
estimated. A failed emit fails `verify`.

## 5. What verify reports

The fidelity leg of a sampled emit says `"sampled": true` beside its status,
and its line says how many blocks and records were read and what the
estimates found. `artifacts["sample"]` holds the request
(`fraction`, `blockRecords`, `seed`) and, per emit, `status`,
`blocks: {read, of}`, `records: {read, of}`, `properties` (each with
`propertyId`, `columns`, `outcome` and, per group by ordinal, `outcome`,
`estimate`, `interval`, `level`, `blocks` and `records`), `notEstimated` and
`refusals`. An estimate and its interval are exact rationals in the column's
own units (the encoded mean divided by `2^fractionalBits`), spelled as
strings as the engine's documents spell exact numbers: in decimal when the
decimal ends, else as `numerator/denominator`. The level is `0.95`. A
refusal names a row by its place in the emit, as the full comparison does.

A sampled verify forms no scorecard: its scorecard leg is `not checked`, and
the scorecard's `intervals` slot stays `not computed` (SCORECARD-SPEC.md
§8). So, as for a pinned run handed no profile, it checks neither the plan's
scorecard thresholds nor an emit's `minCredibility`; `verify` without
`--sample` and `ship` do.

## 6. Coverage

The interval is formed for 95 % coverage, and its rounding only widens it.
The tests hold it to that: over a hundred seeds, a 5 % sample of whole
blocks covers the full mean at least ninety times, through the engine's own
block choice and reading as well as through the estimator alone. Ninety, not
ninety-five, because a correct 95 % interval covers at least ninety of a
hundred with probability 98.9 %, and at least ninety-five only 62 % of the
time.

The interval is the linearised one, which with few blocks covers a little
less than 95 %. With five blocks of a hundred, it covered 93 to 95 % of the
time for a mean over every record, and 91 to 94 % for the mean of a group
whose share of the records varies from a fifth to four fifths between
blocks (4,000 samples of each of three such populations).

## 7. Later additions

- Proportions: the shares of level and bin tables and a support's
  non-members are ratios too, and take the same estimator.
- Partitions in place of blocks, once emits are written in partitions with
  a map of them, so a sample is read without scanning a file.
- A sampled `ship`, whose scorecard would carry the intervals.

## 8. Prior art

- **samplics** 0.6.0 (MIT), `estimation/expansion.py`: the ratio's
  linearised scores `(y - x * R) / sum(x)` and the between-cluster variance
  with a finite-population correction, the form §3 writes out. Used as a
  pattern only; it is not a dependency.
- **Abramowitz and Stegun**, Handbook of Mathematical Functions, 26.7.3 and
  26.7.4: the closed form of the t distribution the table is computed from.
- **SQL `TABLESAMPLE SYSTEM`** (DuckDB, PostgreSQL): sampling whole blocks
  rather than rows, for the same reason: reading a block costs no more than
  reading one of its rows. Neither chooses whole partitions or carries an
  interval.
