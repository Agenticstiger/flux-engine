# ADR 0025: dates follow seasonal rates, dwell times follow a state, and children follow their parent's exposure

## Status

Accepted 2026-11-22. Builds on [ADR 0024](./0024-dates-follow-their-parent-rows-lifetime.md), whose dates
stream, positions and draws it extends, and on the expected-count hook of ADR 0016 (children per parent are
counted), through which a parent's exposure sets its number of children. Its module facts live
under [ADR 0018](./0018-module-facts-live-in-the-flux-bundle.md)'s namespace.
**Amended 2026-11-23:** profile format `1-3-0` and request format `1-1-0` shipped in 0.10.0.

## Context

ADR 0024 puts a date inside the lifetime of the row its link names. Three things it leaves as they were:

- **A date has no calendar.** A date set by a `within` entry or a duration is drawn as an offset from a
  start, or uniformly over an interval. Invoices then fall on a Sunday as often as on a Monday, and a
  timestamp at three in the morning as often as at ten. A column drawn from its own histogram over days
  keeps the months its histogram measured, and loses the hour of the week.
- **A duration ignores the row's state.** A lifetime's duration is one histogram for every row, yet a
  ticket's time to close depends on its priority and a subscription's length on its plan. The time a row
  spends in a state before it changes depends on that state.
- **A child ignores how long its parent is active.** A child picks its parent uniformly, by a weight, or
  by ADR 0016's counted layout, so a customer active for two months may hold as many invoices as one active
  for five years.

FLUID has no spelling for any of the three, so they are module facts in ADR 0018's sense.

Borrow-before-build search, 2026-11-22. The codebase came first: KERNEL-SPEC-2 section 5.4 specifies an
exponential in binary64 basic operations, ADR 0024 section 6 reserves positions 1 to 256 of the dates
stream for this record, its section 4 the duration histogram, and ADR 0016 section 3 the expected-count
hook. Searches: "Lewis Shedler 1979 thinning Naval Research Logistics Quarterly", "nhppp R package
nonhomogeneous Poisson thinning inversion piecewise constant", "hsmmlearn explicit durations", "Salmon
Moraes Dror Shaw Parallel random numbers as easy as 1, 2, 3", "thinning expected number of candidate
points majorizing rate", "synthetic data timestamps day of week hour of day seasonal event table"; the
pages, package indexes and licence files below were fetched on that day.

| Name | What it does here | Licence | Taken |
|---|---|---|---|
| Lewis and Shedler, "Simulation of nonhomogeneous Poisson processes by thinning", Naval Research Logistics Quarterly 26(3), 1979, 403 to 413, doi 10.1002/nav.3800260304 | candidates from a process at a dominating rate, each kept with probability `lambda(t) / lambda_max` | paper | the acceptance rule, in the engine's own fixed-count, counter-addressed implementation |
| nhppp 1.1.1 (CRAN; arXiv 2402.00358) | nonhomogeneous Poisson sampling by inversion, by order statistics, and by thinning under a piecewise-constant majorizer | GPL (>= 3) | pattern only: a piecewise-constant intensity bounded by one constant; its draw counts vary per event, the engine's do not |
| Synthea (master-branch-latest, 2026-11-20) | lifecycle state machines whose Delay states draw a duration as exact, a range, or a distribution, an exponential with a mean among them | Apache-2.0 | pattern: a dwell is a histogram or an exponential with a mean, chosen by the state |
| pyhsmm 0.1.7, edhsmm 0.2.1, hsmmlearn 0.1.0 | explicit-duration semi-Markov models, a duration distribution per state | MIT, MIT, GPL-3.0 | pattern only: durations keyed by state; these fit models, the engine only draws |
| statsmodels 0.15.0, `Poisson(exposure=...)` | the log of the exposure enters the linear predictor with coefficient 1, so the expected count is proportional to the exposure | BSD-3-Clause | the semantics of an expected count proportional to active time |
| Prophet 1.5.0 | yearly, weekly and daily seasonality as components that may multiply | MIT | pattern: month, day of week and hour of week as factors of one product |
| Random123; Salmon, Moraes, Dror and Shaw, SC11, 2011 | the counter-based generator family Philox belongs to, a draw addressed by its counter | BSD-3-Clause | already the kernel's: a fixed proposal count keeps every row's positions fixed |
| SDV 2.2.0, Mimesis 22.2.0, Faker 40.63.0 | no seasonal event-time draw; Mimesis tutorials weight business hours on top of a draw | BUSL-1.1, MIT, MIT | none |

No tool found thins with a fixed number of proposals on a counter-addressed generator. For a piecewise-constant
intensity, inversion is exact with one draw, but here the base of every row is its own (an offset from its
parent's start, cut to its own interval), so inverting would build a cumulative table per row whose size grows
with the interval: a timestamp over five years crosses forty thousand hours. Thinning costs, per row, the
ratio of the dominating rate to the mean rate in proposals, whatever the interval's length.

## Decision

### 1. Kernel 0.2.0 only, and only where a bundle asks

Everything below is read from keys the engine did not read before 0.10.0: the namespace's `rates`, a
lifetime duration's `given`, `states`, `otherwise` and `mean`, and a `within` entry's `exposure`. A bundle
that declares none of them generates, plans and digests exactly as before, under both kernels, and as ADR
0024 has it. Under kernel `0.1.0`, `rates` is refused with `FLX-3105`, naming the kernel, as `lifetimes`
and `within` already are. Each entry is bound into the declaring emit's entry for `H`, present only when
the emit declares it, and `G` leaves it out.

### 2. Seasonal rates

The namespace key `rates` lists `{emit, column, month, dayOfWeek, hourOfWeek}` entries, at most one per
column of an emit. `column` is a DATE or TIMESTAMP column of the emit that a `within` entry or a
lifetime's `duration` sets, so a column ADR 0024 draws; any other column is `FLX-3105`. Each factor is an
optional list of weights written as decimal strings of at least zero, not all zero, and at least one of the
three is given:

- `month`, 12 weights, January first;
- `dayOfWeek`, 7 weights, Monday first, as ISO 8601 numbers the days;
- `hourOfWeek`, 168 weights, from Monday 00:00 to 00:59 UTC first, for a TIMESTAMP column only, since a
  date has no hour (`FLX-3105` on a DATE column).

The intensity of a value `t` is `lambda(t) = month[m(t)] * dayOfWeek[d(t)] * hourOfWeek[h(t)]`, read in UTC,
a missing factor counting as 1; a DATE value is its day. Its bound is `lambda_max`, the product of each
given factor's largest weight, so no value's intensity exceeds it.

**The draw.** The value ADR 0024 draws for the column (the duration's, the offset's, or the uniform one)
becomes proposal 0 of `K = 128`. Proposal `k` draws its value at position `1 + 2k` with the sampler the
direct draw uses, and is tested at position `2 + 2k` with one `uniform53`, `a`: it is accepted when
`a * lambda_max < lambda(value) * 2^53`, on exact rationals. The value is the first proposal accepted. A
proposal whose value is NULL (an `open` slot, or an end past the window under `endPastWindow: open`) has no
date to weigh and is accepted at once. When no proposal is accepted the value is proposal 0, traced as a
rates fallback; that happens with probability at most `(1 - lambda_min / lambda_max)^128` on a row whose
interval only holds values of intensity `lambda_min`, and always when every value of its interval has
intensity zero. A row stops at its first accepted proposal and leaves the later positions untouched, so no
row's words depend on another row's. A period's end is thinned before it is cut at its parent's end, and
a violating row's value (ADR 0024 section 7) is a candidate, never a proposal.

### 3. Dwell times given a state

A lifetime's `duration` (ADR 0024 section 4) is a dwell: a histogram as written there, or an exponential
`{mean, open}`, `mean` the mean duration in days as a decimal string above zero and `open`, optional, the
share of rows with no end as a decimal string from 0 up to below 1. Or it is a dwell family:
`{given, states, otherwise}`, where `given` names a column of the emit, the state; `states` maps a value's
text to a dwell, at most 1 024 of them; and `otherwise` is the dwell of a row whose state is NULL or not
listed. Without `otherwise` such a row keeps the end its own cell drew, as a row whose start is NULL does.

- **An exponential** draws one `uniform53`, `u`, at position 1 and takes KERNEL-SPEC-2 section 5.4's
  exponential at scale 0 in the end column's units, with rate `1 / mean` in those units: whole days for a
  DATE end, milliseconds for a TIMESTAMP end. Its rate's refusals are section 5.4's (`FLX-5021`). With an
  `open` share `W` and `c = floor(2^53 * W)`, a `u` below `c` is no end, and otherwise the exponential reads
  `floor((u - c) * 2^53 / (2^53 - c))`, which lies in `[0, 2^53)`. Where the end column declares a range (ADR 0024 section 4), the dwell is cut to the days that keep the
  end inside it, exactly: the least and the greatest `v` in `[0, 2^53)` whose exponential value lies in that
  interval are found on the monotone transform (a binary64 estimate starts the search and the boundary is
  the exact one), and the read value `v` is `v_min + floor((u - c) * (v_max - v_min + 1) / (2^53 - c))`, a
  `u` that is not below `c` mapped onto that span in place of the whole `[0, 2^53)`.
- **The state** is the row's final value of `given`, written as its plain text in NFC (a string as stored, a
  date as `YYYY-MM-DD`, a timestamp as its epoch milliseconds, a number as its canonical decimal, a boolean as
  `true` or `false`), which is how a `states` key is written, so the end is ordered after it. A `given` that is the end, or a column the end's value decides, is a cycle
  (`FLX-6208`).
- **A reusable draw.** `flux_engine/generator/dwell.py` holds the family and its draws:
  `draw_family(family, state, u53, unit)` says whether a member applies and gives its value, and
  `draw_dwell(dwell, u53, unit)` draws one member, in whole units or `None` for no end. A mean or an
  `open` share that is not a finite decimal is refused, so every family can be written as it was read. A version's
  duration given its state (versioned entities) is the same draw on the version's own stream.

The profile's dwell parameters are the `datePairs` statistics of ADR 0024 section 9 grouped by a column:
`fluxsim init` writes that column as `given`, one histogram per published group as `states`, and the
ungrouped histogram as `otherwise`.

### 4. Children follow their parent's exposure

A `within` entry over one relationship may carry `exposure: true`. Its parent's exposure is the part of the
parent's lifetime inside the run's window, measured in days and weighted by the child column's intensity
when the column has a `rates` entry: with `Lambda` the integral of `lambda` from the window's start,
`E = Lambda(b) - Lambda(a)`, `[a, b]` the lifetime cut to the window, an exact rational since `lambda` is
constant on each hour (on each day for a DATE column). A lifetime that is open runs to the window's end; one whose end is before its start
reads as open, as in ADR 0024; one outside the window, or whose start is NULL, has exposure zero. A run
without `spec.timeBounds` has no window to measure in, and `exposure` there is `FLX-3105`, and so is
`exposure` on a chain of relationships, since a row is counted among the children of the row its own link
names.

The exposure is the parent's expected count in ADR 0016 section 3's hook, `ExpectedCountHook`: `member`
returns the entry's declaration, which binds into the child plan's digest, and `expected` returns `E` from
the parent's final row. It draws nothing and moves no position. Each parent's number of children then
follows its exposure as that section allots expected counts, in proportion without a `children` shape and
by rank with one, and each child is dated inside its parent's interval by its `within` entry. A customer
active for two months gets about a thirtieth of the invoices of one active for five years, and none when
it is never active inside the window.

### 5. The stream and its positions

The rule stream is ADR 0024's, `R` the canonical JSON of `["dates", <column>]`, and its value takes the
256 positions that record reserved:

| position | what it draws |
|---|---|
| 0 | `bernoulli(1 - s)`, whether the row holds, only for a `target` above zero (ADR 0024) |
| `1 + 2k`, `k` from 0 to 127 | proposal `k`'s value; proposal 0 is ADR 0024's direct draw, and a column without `rates` draws it alone |
| `2 + 2k` | proposal `k`'s test, one `uniform53` |
| `257 + j * m` | violation candidate `j` (ADR 0024) |

A column with no `rates` entry draws position 1 and nothing more, so its rows are ADR 0024's. The proposals
and tests at positions 2, 3 and 256 are pinned in `tests/vectors/kernel2-dates-vectors.json`, and no existing
vector moves.

### 6. Profile format 1-3-0 and request format 1-1-0

Both formats were unreleased when this record was written and shipped in 0.10.0, so they gain members rather than versions:

- **`tables.<id>.calendar`**: `{column, month, dayOfWeek, hourOfWeek}` for a DATE or TIMESTAMP column, each
  a `levelHistogram/1` of the column's known values over its levels, `01` to `12`, `1` to `7` (Monday is
  `1`) and `000` to `167` (Monday 00:00 to 00:59 UTC is `000`), written with leading zeros so that their
  code-point order is their number's; `hourOfWeek` for a TIMESTAMP only.
- **`relationships[].exposure`**: `{start, end, children, activeDays, window}` for a parent's lifetime
  columns: the number of child rows whose parent's start is known, and the sum of the active days inside
  `window` over every parent row whose start is known, two `count/1` metrics under the profile's
  disclosure rules. A profile states no window of its own, so each entry states the one it measured
  over, `{start, end}` in epoch milliseconds.

The request asks for a calendar of every date column of a table and for the exposure of every pair of a
parent's date columns across a relationship, each bounded, and a request that asks for none of ADR 0024's
or this record's members is still written as `1-0-0`.

### 7. `fluxsim init`

From a profile that publishes them, `fluxsim init` writes:

- a `rates` entry for a column a `within` entry or a duration sets, whose published `calendar` it divides
  level by level by the number of times that level occurs inside the window it writes (days of each month,
  days of each weekday, hours of each hour of the week), so a uniform base reproduces the published counts;
  a level the window does not hold takes the weight 1, a level whose cell is suppressed takes the smallest
  weight the histogram publishes, since a suppressed count is a small one, and each factor is scaled so its
  largest weight is 1 and written to six significant digits;
- `exposure: true` on a `within` entry whose relationship publishes an `exposure` for the lifetime it
  writes;
- the dwell family of section 3 from grouped `datePairs`.

Every published member it does not use is recorded as unused with its reason (ADR 0014).

### 8. Checks

`fluxsim verify` scores two kinds of line, each passing or failing:

- `calendar:<column>` for each published `calendar`: the run's counts of the column by month, by day of week
  and by hour of week against the profile's, by the total variation distance a level histogram is scored
  with, and the tolerance the scorecard gives a level histogram. The line fails the run only for a column a
  `rates` entry shapes; on any other column, which `fluxsim init` cannot shape, it is printed with its
  distances as not gated and its scorecard rows keep their outcome;
- `exposure:<relationship>` for each `within` entry with `exposure: true` whose relationship publishes an
  `exposure`: the run's children per active day against the profile's `children / activeDays`, within the
  relative tolerance of a `facts` entry when one is given and 10 % otherwise.

A column or a parent that is hidden is listed as not scored, with that reason, as ADR 0024 section 8 lists a
fact over hidden data.

The scorecard's `temporal` dimension scores each calendar histogram of those lines as a row of a `calendar`
property kind, `1 - distance`, in scorecard format 1-4-0, which the counted layout introduced in this
release; this record extends that format and does not define another. A run whose profile publishes no
calendar writes the scorecard it wrote before, format name included.

## Consequences

- A bundle that declares none of the keys keeps every byte and digest, under both kernels; a bundle with
  ADR 0024's keys and none of these keeps ADR 0024's.
- A row under rates takes, on average, `lambda_max` over its interval's mean intensity in proposals, each
  one value draw and one test; strongly peaked rates cost more draws, never more positions.
- Counts that follow exposure need ADR 0016's counted layout: a relationship whose `within` entry declares
  `exposure` is counted.

## What would change this decision

- Rates that vary by year, or a trend, would add a factor; a factor that is not a product of the three
  would need inversion of a declared intensity table, which needs a bound on its size.
- A `rates` entry on a column drawn from its own statistics would thin that column's own draw on the dates
  stream, with its own statistics as the base.
- A measured dependence of children on exposure that is not proportional (a saturating one) would need an
  exponent on `E`, which the profile would have to measure.
