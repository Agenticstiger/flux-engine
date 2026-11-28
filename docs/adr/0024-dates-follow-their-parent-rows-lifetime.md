# ADR 0024: dates follow their parent row's lifetime: hidden columns and tables, lifetimes, dates within a linked row's lifetime, and measured violation shares

## Status

Accepted 2026-11-22. Builds on [ADR 0022](./0022-rows-read-their-linked-rows.md), whose parent reads
it uses, and on [ADR 0018](./0018-module-facts-live-in-the-flux-bundle.md), under whose namespace its
module facts live. It draws as EXPR-SPEC-2 section 6 specifies a bounds draw relative to a parent value
(`distribution`) and as section 7 specifies the shares below 100 %, on the rule stream section 7 names,
so the bounds form over a relationship of [ADR 0011](./0011-rows-follow-their-key-entity-and-parent.md)
step 3b draws the same way when it comes. That form itself stays `FLX-6210`.
**Amended 2026-11-23:** profile format `1-3-0` and request format `1-1-0` shipped in 0.10.0.

## Context

FLUX draws each date column of a row from that column's own statistics. Nothing ties a date to the row
its link names, so on the engine today:

- **A child is dated outside its parent's lifetime.** With customers that start on a date and may end on
  another, and invoices that name a customer, an invoice is dated before its customer started, or after
  its customer ended, as often as the two marginals overlap by chance.
- **A chain does not hold.** An invoice names an account, and the account names a customer. Even when
  each level were right on its own, nothing makes the invoice fall inside the customer's lifetime.
- **An end is a second marginal.** A ticket's close date is drawn apart from its open date, so it closes
  before it opens, and a row that ends after the run's window is given an end the window never saw.
- **Related rows have no order.** A payment can be dated before the invoice it pays.
- **Real data is not clean, and that is measured.** Late and backdated records exist. A generator that
  removes every one of them is as unfaithful as one that ignores the order.
- **Every column is written.** A lifetime may rest on a date the contract does not publish (a customer's
  end), or on a table the run does not publish (the customers behind the accounts it writes). FLUX can
  neither draw a column it does not write nor generate a table it does not write, and a relationship
  into an expose the Simulation does not generate is `FLX-3403`.

FLUID has no spelling for a lifetime, for a date inside another row's lifetime, or for a column that is
drawn and not written. Its `validityParams` (`isStart`, `isEnd`) name the validity window of an entity's
versions, which is a different fact (a version ends where the next begins) and the subject of a later
record. So the declarations below are module facts in ADR 0018's sense.

Borrow-before-build search, 2026-11-22. The codebase came first: EXPR-SPEC-2 sections 6 and 7 already
specify the draw relative to a parent value and the shares below 100 %, ADR 0022 the parent's final value,
ADR 0023 the integer thresholds of a weighted pick, and `flux_stats` the histograms a profile publishes
over days. Searches: "PostgreSQL 18 temporal foreign key PERIOD WITHOUT OVERLAPS", "SDV Inequality
constraint", "synthetic data generator child date after parent date foreign key temporal consistency";
fetched the PostgreSQL documentation pages and the projects' licences on that day.

| Name | What it does here | Licence | Taken |
|---|---|---|---|
| EXPR-SPEC-2 sections 6 and 7 (this repository) | a value drawn as a histogram of `c - p` or `DAYS_BETWEEN(p, c)` cut to an interval on integer thresholds; a violation share drawn at position 0 of the rule's stream, with 16 candidates | Apache-2.0 | reused: the draw, the positions and the stream, as specified |
| ADR 0022 section 3 (this repository) | a path reads the parent's final value, its own rules included | Apache-2.0 | reused: a parent's start is its final value, so a chain holds level by level |
| PostgreSQL 18, temporal foreign keys (`FOREIGN KEY (..., PERIOD valid_at)`, `WITHOUT OVERLAPS`; docs for 18, `ddl-temporal-tables` and `sql-createtable`) | SQL:2011 temporal referential integrity: the referenced history must cover the referencing row's period; an omitted end is unbounded; ranges include their start and exclude their end | PostgreSQL Licence | the semantics of "covered by its parent's period", with one difference stated below |
| SDV 2.2.0, `Inequality` (docs) | `low_column_name < high_column_name` on every row, missing values ignored, strict by default | BUSL-1.1 | pattern only, as ADR 0003 requires: an end is drawn as a positive gap from its start, NULLs pass |
| Samyama-SDG 2.1.0 (arXiv 2611.11295; repository Apache-2.0) | an `@after` directive, resolved across several hops from a seekable generator without holding parent tables | Apache-2.0 | none needed: ADR 0022 already reads a parent row from its streams |
| Misata 0.9.8.1 (PyPI) | tables ordered by their dependencies so a child reads its parent's start date | MIT | pattern only, already the engine's rule order |

No tool found measures how often real data breaks a date order and generates that share, and none keeps
a chain inside every level's lifetime by reading each parent's final value.

## Decision

### 1. Kernel 0.2.0 only, and only where a bundle asks

Everything below is read from keys the engine did not read before: the namespace's `lifetimes` and
`within`, and an emit block's `hidden` and `hiddenColumns`. A bundle that declares none of them generates,
plans and digests exactly as before, under both kernels. Under kernel `0.1.0` each of the four is refused
with `FLX-3105`, naming the kernel, as ADR 0022 section 1 refuses its keys. Each new key that changes an
emit's rows is bound into that emit's entry for `H`, present only when the emit declares it, and `G`
leaves it out, as it leaves out `links`.

### 2. Hidden columns

An emit block's `hiddenColumns` lists `{name, type}` entries: a column the expose's contract does not
declare, of a FLUID logical type written as the contract writes one. A hidden column is planned as a
contract column is, after them in the order listed: it has statistics in the emit's `fidelity` block,
a rule may set it, a path may read it, and a lifetime may name it. It is drawn on the generator's cell
path with its own name (SEAM-FIDELITY-SPEC section 1), a path no existing column draws on, since a
hidden name that is a contract column's is refused (`FLX-3105`). It is never written: it is in no stream,
no header, no `outputDigest` and no sink.

### 3. Hidden tables

An emit block's `hidden: true` makes the emit a hidden table. It is planned and generated as any emit,
its rows are what a path through it reads, and it is never written: it has no stream, no file and no
`outputDigest`, and its plan entry says it is hidden. A relationship into it, and so a lifetime that
rests on it, is generated as a relationship into a written expose is. A hidden table that no written
emit reads through a link is refused (`FLX-3105`), since nothing would use its rows.

### 4. Lifetimes

The namespace key `lifetimes` lists `{emit, start, end, duration, endPastWindow, target}` entries, at
most one per emit. `start` names a DATE or TIMESTAMP column of the emit, and `end` optionally another of
the same kind, either one hidden or not. The row's lifetime is the closed interval from its start to its
end, its last day included, and it is open when the end is NULL or when the entry names no end. Its
fact is that the end is not before the start. A lifetime with a start and no end is how a row that only
begins is declared, such as an invoice that a payment follows. A row whose end is before its start
violates its own lifetime, which its `lifetime:` line counts once; for every row that reads it as a
parent, its lifetime is open.

With `duration` the end is drawn as the start plus a duration in whole days:
`{bins, binWeights, closed, open}`, a histogram as EXPR-SPEC-2 section 6 writes a `distribution`, over
the days from the start to the end, and `open`, an optional weight beside the bins for the rows whose
lifetime does not end. `endPastWindow` says what an end after the run's window (`spec.timeBounds.end`)
is: `open`, the end is NULL, because the window never saw it; or `kept`, the end keeps the drawn date,
a scheduled end such as a subscription's validity has. It defaults to `open` for an end column the
contract leaves nullable and to `kept` for one it does not, and `open` on a column the contract
requires is `FLX-3105`. An `open` weight on a `kept` end is `FLX-3105` too. Without `duration`, the end is
drawn from its own statistics and the fact is checked, not generated.

**The end stays inside its column's own range.** Let `Re` be the range the end column's declared statistics
give it (`range: {min, max}`; for a `marginalHistogram` its first and last edges), the support a value
written to the column is checked against (`FLX-5005`). A column whose statistics bound it has no `Re`, and
nothing here changes for it. The duration is cut so that the start plus the duration stays inside `Re`: the
day interval `[Re.min - start, Re.max - start]` meets `[0, inf)`, and the histogram is cut to it at the end
column's grain through the cut arithmetic of section 5 (a bin keeps the part of its weight its values inside
the interval hold, the `open` weight stays whole), still one `uniform53` at position 1. An exponential
dwell (ADR 0025 section 3) is cut exactly: the least and the greatest `uniform53` whose exponential value
lies in the interval bound a span, and the draw maps `u` onto that span. Where no value fits on a row, a
nullable end under `endPastWindow: open` is NULL, so the lifetime is open. Anywhere else a run never meets
such a row, because `plan` refuses it (`FLX-6204`) whenever `s_max + d_min > Re.max`, and, as the mirror,
whenever `s_min + d_max < Re.min`, with `s_min` and `s_max` the declared bounds of the start column and
`d_min` and `d_max` the shortest and the longest duration the entry can draw, an exponential having no
longest. The message names the remedies: widen the declared range of the end, change the duration, or
declare `endPastWindow: open` on a nullable end.

### 5. A date within a linked row's lifetime

The namespace key `within` lists `{emit, relationship, date, lifetime, offset, target}` entries.
`relationship` is a to-one relationship of the emit, or a chain of them written with dots as a FLUX-side
path is (EXPR-SPEC-2 section 3), whose last target declares a lifetime. An entry names exactly one of
`date`, a DATE or TIMESTAMP column of the emit, and `lifetime: true`, the emit's own lifetime.

- **The fact for a single date** `c`, with `p` the row its link reaches: `p.start <= c`, and `c <= p.end`
  unless `p`'s lifetime is open. The rows it covers are the rows whose link reaches a row whose start is
  not NULL; a row whose parent start is NULL is not covered and keeps the value its own cell drew.
- **A DATE against a TIMESTAMP.** A lifetime includes its last day, so a DATE start bounds a TIMESTAMP
  from its midnight UTC, and a DATE end bounds it up to the end of that day, before the next midnight UTC.
  A TIMESTAMP bounds a DATE by the day that holds it.
- **The fact for a period**, `lifetime: true`: the emit's start is within `p`'s lifetime as above, and
  when `p` has ended the emit's end is not NULL and not after `p.end`.
- **The interval.** A date is drawn inside `J`, `p`'s lifetime cut at the run's window: the part of the
  lifetime inside `spec.timeBounds` when there is one; otherwise, when the lifetime ended before the
  window, the whole lifetime; otherwise, when it starts after the window, `p.start` alone. So the
  lifetime always wins over the window, and a date lies outside the window only where its parent's
  whole lifetime does. Since a parent whose end is before its start reads as open, `J` is never empty
  on a covered row.
- **The column's own range.** A date is drawn inside `J ∩ R`, with `R` the range the date column's own
  declared statistics give it (`range: {min, max}`; for a `marginalHistogram` its first and last edges), in
  the column's units, so that a written value never leaves the support `FLX-5005` checks it against. A
  column whose statistics bound it has no `R`, and its draw is the one below on `J`. The cut histogram and
  the uniform draw below run over `J ∩ R`, with the same samplers and positions, and so do the proposals of
  seasonal rates (ADR 0025 section 2). On a row where `J ∩ R` is empty the date keeps the value the
  column's own cell drew, which its own statistics drew inside `R`, nothing more is drawn, and the row
  counts as `infeasible` for the entry (section 7), so it counts toward the fact's violations and `verify`
  reports it as one.
- **The draw.** With `offset`, a histogram of `DAYS_BETWEEN(p.start, c)` written as a `distribution`
  (bins, binWeights, closed), the date is the value EXPR-SPEC-2 section 6 draws inside `J ∩ R` from that
  histogram, one `uniform53` at the column's grain. Without it, and on a row whose bins leave no value
  inside `J ∩ R`, the date is uniform over `J ∩ R` at the column's grain, one `bounded` at the same position.
  Which of the two samplers a row takes depends on `J` and the bins, never on a draw, and a row that
  falls back from its offset is traced as an offset fallback.
- **Several entries for one column** name relationships along one chain, each a prefix of the longest
  (`account` and `account.customer`), and at most one of them carries an `offset`; anything else is
  `FLX-3105`. Their lifetimes are intersected first and the intersection is cut at the window by the
  rule above, so entries that meet inside the window and entries that meet before it both draw. The
  lifetimes along one chain fail to meet only where a middle row leaves its own parent, which only a
  `target` on that row's entry allows. So `plan` refuses an enforced entry that another entry's target
  can make unmeetable (`FLX-6204`, as EXPR-SPEC-2 section 7 refuses a band that zero violations would
  fail). When both carry a target, a row whose lifetimes do not meet is drawn inside the nearest link's
  interval, counts as a violation of the outer entry, and is traced as infeasible.
- **An enforced entry the declared ranges can leave empty.** `plan` refuses an enforced entry (one with no
  target) when the declared ranges make an empty `J ∩ R` possible on some row (`FLX-6204`). With `W` the window, `Rs` the declared range of the parent's
  start column and `e_min` the smallest end the parent's lifetime can take (`Rs.min` plus the shortest
  duration its entry can draw, or the parent's end column's declared minimum, or no bound for a lifetime
  that has no end), `J ∩ R` is never empty when `Rs.max` is at most `R.max`, `e_min` is at
  least `max(R.min, W.start)`, and `R` meets `W`. The check is sufficient, not necessary: it refuses a
  bundle it cannot prove, and the message names both remedies, to widen the column's declared range or to
  give the entry a `target`. For several entries on one column it runs on the intersected levels
  conservatively, with the latest possible start and the earliest possible end. For a period it runs on
  the start so, and on the end against the end column's range whenever a parent's end can fall below the
  range's minimum. An entry with a target is not refused: its empty rows are counted `infeasible`.
- **A period.** For `lifetime: true` the start is drawn as above, and the end is drawn as section 4 says,
  then cut at `p.end`: when `p` has ended, an end after `p.end`, or an open one, becomes `p.end`, so a
  child ends with its parent. The end column's own range `Re` still holds: the duration is cut to it as section 4 says before the
  parent's end cuts it, and an end that the parent's end puts below `Re.min` is `infeasible`: the row keeps
  the end its own cell drew, inside `Re`, and counts as a violation of the period entry whose parent ended
  first. Past the window an end follows the lifetime's `endPastWindow` only while
  `p` is still active there.
- **Order between related rows** is the same entry. A payment after its invoice is a `within` entry on
  the payments' date through the invoice relationship, where the invoices declare a lifetime that starts
  at their date and has no end. A ticket that closes after it opens is the ticket's own lifetime.

**Chains hold level by level.** A parent's start and end are its final values (ADR 0022 section 3), so a
parent that is itself within its own parent is drawn there first. An invoice within its account's
lifetime, whose account's period is within its customer's (`lifetime: true`), falls inside the customer's
lifetime on every row that violates neither level. A tie on the account's start alone keeps the invoice
after the customer's start and not before its end, so it does not carry the outer level. An entry may also
name the chain itself (`account.customer`) to state the outer level directly.

The rows of the two related exposes are read as ADR 0022 reads them: the plan of a reading expose carries
the plan of every expose its `within` entries reach, and a cycle among them is `FLX-6210`. A date set by
`within` or by a duration is a column a rule determines: it keeps its declared NULLs first (EXPR-SPEC-2
section 6), it is ordered after the columns it reads, and a contract `derive` that reads it, such as a
status from the end, reads its final value.

### 6. The stream and its positions

Each column that section 4 or 5 sets draws on the rule stream of EXPR-SPEC-2 section 7,
`<row path> + ("rule", R)`, with `R` the canonical JSON of `["dates", <column>]`. Its first element is
no element a contract or a bundle rule's `R` holds, so the path is one no other draw has, and under
kernel `0.2.0` its key leaves the row out and its counter carries it. Its positions are section 7's,
with a value that takes `v = 256` positions: position 1 for a direct draw, and 1 to 256 for the thinning
of seasonal rates that the record after this one adds. Its candidates then sit at section 7's
`1 + v + j * m`, which is `257 + j * m`:

| position | what it draws |
|---|---|
| 0 | `bernoulli(1 - s)`, whether the row holds, only for a `target` above zero |
| 1 | a direct value: the offset's or the duration's `uniform53`, or the uniform `bounded` |
| 1 to 256 | the value under seasonal rates, the next record's; untouched here beyond position 1 |
| `257 + j * m` | violation candidate `j`, from 0 to 15, each taking the `m` calls one draw from the column's own statistics makes |

A row that does not need a position leaves it untouched, so no draw's words depend on what another row
needed. The new path's key, counters and blocks are pinned in a vector file of their own,
`tests/vectors/kernel2-dates-vectors.json`, as KERNEL-SPEC-2 section 12 asks, and no existing vector moves.

### 7. Measured violation shares

An entry's `target` is the percent of covered rows that violate its fact, as a decimal string, as
EXPR-SPEC-2 section 7 writes a rule's target. On a covered row whose own value is not NULL, position 0
decides whether it holds, with `s` the target as a share once the NULLs are taken off (EXPR-SPEC-2
section 6). A violating row takes the first of its 16 candidates, drawn from the column's own
statistics, that lies outside the interval, and otherwise the first end of its own declared range that
does; one that finds none keeps a value that holds and is traced as not violated. For a period the
violation is drawn on the start. Every violating row is traced as a violation injected on purpose. With
no target the entry generates no violation, and every row it covers holds: an enforced fact fails only
on a defect, never on a row the generator made.

### 8. Checks

`fluxsim verify` scores each fact as a line of its `rules`: `lifetime:<start>.<end>` for each lifetime,
`within:<relationship>.<column>` for each `within` entry (`within:<relationship>.lifetime` for a period),
and, for each chain of entries, the outer level as `within:<relationship>.<relationship>.<column>`, so
every level of a chain is scored. A fact with no target is enforced: a violating row fails the run, as a
`derive` without a band does. A fact with a target is checked against it. The implied outer level of a
chain is checked against the sum of its levels' targets, zero when every level is enforced, unless an
entry states that outer level itself, whose own target then holds. An outer level is implied through period
ties only: a middle expose tied by a date entry on its start makes no implied line, and the chain goes on
only through period ties. Each fact is named
`<exposeId>.<ruleId>` with these ids, so a `facts` entry may give it a tolerance, the profile's
`invariants` section may hold its measured share, and the scorecard's `invariants` dimension scores it
as ADR 0018 scores any fact.

The run's streams hold no hidden column and no hidden table, so a fact that reads either is listed by
`fluxsim verify` and by the evidence pack as not scored, with that reason, and never counts as passing.
Verify reads the rows a run wrote and does not generate rows, which keeps it the independent check it is;
regenerating hidden rows inside it would check the generator against itself.

### 9. Profile format 1-3-0 and request format 1-1-0

Profile format `1-3-0` was not released when this record was written and shipped in 0.10.0, so it gains two optional members rather than a new format:

- **`tables.<id>.datePairs`**: `{start, end, statistics, order}` for two DATE or TIMESTAMP columns of one
  table, over the rows where both are known. `statistics` are of `DAYS_BETWEEN(start, end)`
  (`marginalHistogram/1`, `quantile/1`, `quantile/2`), which may be grouped by a column of the table
  (`groupBy`); `order` is a `levelHistogram/1` over `["before", "same", "after"]`, the end against the
  start.
- **`relationships[].dates`**: `{parent, child, statistics, order}` for a DATE or TIMESTAMP column of the
  parent and one of the child, over the joined rows where both are known: `DAYS_BETWEEN(parent, child)`
  and the child's order against the parent, in the same forms.

Request format `1-0-0` is released, so the request gains a new minor format, `1-1-0`, which asks for the
same two members: every pair of date columns of a table, and every pair of a parent's and a child's
date columns across a relationship, each bounded. A request that asks for neither is written as `1-0-0`,
byte for byte as before.

### 10. `fluxsim init`

`fluxsim init` declares an order only where the profile shows that it holds on at least 90 % of the
rows where both values are known, the constant `ORDER_HOLDS_AT_LEAST`. Two independent date columns
break even near half, so a lower bar would turn noise into a declared fact with a large target. From a
profile that publishes them, it writes:

- a lifetime for a table's pair whose `order` puts the end not before the start on at least that share,
  the one with the most such rows when several qualify, with the pair's histogram as its `duration`, the
  share that breaks the order as its `target`, and `endPastWindow: kept` when the end column's published
  maximum lies after the window;
- a lifetime with only a start for a parent's date that a relationship's `dates` pair orders a child
  after on at least that share, when the parent's table declares no lifetime;
- a `within` entry for each relationship pair whose parent column starts the parent's lifetime and whose
  child column is not before it on at least that share of the joined rows, with the pair's histogram as
  its `offset` and `order`'s `before` share, plus the `after` share against the parent's end, as its
  `target`; `lifetime: true` when the child column starts the child's own lifetime.

Every published pair it does not use is recorded as unused with its reason, as ADR 0014 requires, a pair
below the bar with the share it holds on.

## Consequences

- A bundle that declares none of the four keys keeps every byte and every digest under both kernels. A
  golden pins a bundle with two date columns and a relationship, its digests computed with the code of
  origin/main, and the same bundle with the keys, whose rows move.
- A date set here is drawn row by row after the row's cells, as every rule over a link is, so the column
  path of kernel `0.2.0` keeps drawing the cells and its rows are the per-row path's, which a test
  compares for each construct.
- A child of a parent whose lifetime lies before the window is dated inside that lifetime, so outside
  the window. Picking such a parent at all is a matter of how many children a parent has, which the
  record on temporal rates takes up.
- Hidden columns and tables cost what any column or table costs to draw, and are never written.

## What would change this decision

- PostgreSQL's ranges exclude their end. A lifetime here includes its end, because a business date that
  ends a lifetime is usually its last day, as an invoice on the day a customer leaves shows. Data whose
  ends are exclusive would need a `closed` setting on the lifetime.
- A FLUID spelling for a lifetime, or for a temporal foreign key, would move the declaration into the
  contract.
- A contract bounds form over a relationship (ADR 0011 step 3b) generates through the same draw when it
  comes; a bundle that writes a date order both ways would then have to choose one.

## Amendment of 2026-11-23: a period end's NULL share under closed parents

Section 5 cuts a period's end at a closed parent's end: an end that is later, or open, becomes the parent's
end. So a period's end is NULL only on a row whose parent is open, and a `nullFraction` declared on the
end column is met only among the rows under open parents. When a share of the parents has closed, the
share of NULL ends the run generates is lower than the declared one, in proportion to the parents that
closed. A declared 0.048 over parents of which about a third are open generates about 1.6 %. This is a
property of the rule and not a defect in a draw, and it is documented rather than changed: a different
cut would move the bytes of every existing bundle with a period, which kernel `0.2.0` and its replays
(KERNEL-SPEC-2 section 12) do not allow.

`plan` and `run` now print one note on stderr for each such end: a `lifetime: true` entry whose emit
declares a `nullFraction` on its end column and whose parent's lifetime names an end. The note names the
emit, the column and the declared share. It is not part of `plan.json`, a digest or a receipt, and `plan
--json` lists it under `period_end_notes` in its artifacts. A bundle that wants a given share of NULL ends
overall declares the share among open parents, which is the declared share divided by the share of
open parents.

## Amendment of 2026-11-25: a date column is drawn within the range its contract declares

A DATE or TIMESTAMP column may carry `validationRules: [{type: range, constraint: "between '2001-01-01'
and '2005-12-31'"}]`. Nothing read it: the column was drawn from its declared statistics, whose bins could
run past the range, and `verify` counted only the rows' number and width, so a run could write most of a
column outside its contract and pass. The profiler refuses the same rows (`FLX-5005`).

**The range is read once, in the profiler's spelling.** `CompiledColumn.declared_range` holds the
`(low, high)` milliseconds `declared_range_bounds` gives (`between 'A' and 'B'`, or `>=`, `>`, `<=`, `<` joined
by `and`; an ISO date is midnight UTC, a timestamp RFC 3339; anything else declares nothing). Each end is
optional: `>= '2024-01-01'` is a range, as a one-sided INT or DECIMAL range is, and a high end in the year
9999 or later is the open-end spelling and so no high end (`between '2024-01-01' and '9999-12-31'` is
`>= '2024-01-01'`). A rule with neither end declares nothing. A range whose ends meet and are both inclusive (`between 'A' and 'A'`) holds that one day or instant. A range that holds no instant (its low end beyond its high end, or equal ends with one strict) is `FLX-3403` at `plan`, naming the column and both ends, on every kernel and for a column something else sets; a recorded plan without `dateRanges` never read it. A timestamp end between two milliseconds is read to the millisecond: a low end rounds up and a high end rounds down, so the range never holds an instant outside what the contract names. `declared_instants`, which `init` reads for its window, keeps its rule: both ends, the high end
before the year 9999. A DATE holds the days from the first at or after the low end to the last at or before
the high end.

A strict end is kept strict: `> 'A'` and `< 'B'` hold the instants beyond `A` and before `B`, so the compiled
range moves a strict end one millisecond inward (for a DATE, the days whose midnight lies inside it) and the cut
and `verify` compare inclusively and exactly. The profiler reads a strict date bound as an inclusive one only to
lay out its bin edges; where forge checks membership (its number ranges, and the dbt tests it generates with
`strictly: true`) a strict bound is strict, and a run that wrote the excluded end would fail forge's own test of
the same contract. `declared_range_bounds` gives each end with its strictness. A strict range that holds no day (`> '2005-01-01'
and < '2005-01-02'` on a DATE) is `FLX-3403` at `plan` when the column is cut.

**The statistics are cut before the draw, on kernel `0.2.0` only.** A column that is drawn from declared
statistics is drawn inside its range, by cutting what it is drawn from, in its own units:

- a declared `range` (a uniform draw, or a rate's start and clamp) is intersected with the contract's;
- a bin is cut at the range's ends and keeps weight in proportion to the width it keeps, so a draw inside a
  kept part is as likely as before; a bin wholly outside is dropped. The last bin of a histogram is closed
  above, so a bin cut at the top ends on the range's last value (a bin cut to one value has equal edges).
  Every weight is multiplied by one whole number, so the weights stay exact decimals and the draw, which
  divides by their sum, is unchanged by it. A `binWeightsGiven` table is cut alike in every level;
- a column with no statistics of its own, drawn within `timeBounds`, has that range intersected too;
- a bin that is cut leaves the `range` the column declared as it was, since a histogram never reads it.

A column whose statistics already lie inside its range is returned as it is, and keeps its plan and its
bytes. Kernel `0.1.0` keeps its bytes. The column path and the per-row path read the cut statistics from the
plan, so they agree under any `--jobs` and any shard. The cut changes `generatorDigest` only for a column it
cuts.

**A recorded plan replays as it was made.** `apply --run-id` and `replay` run the bundle again, so the
engine records the cut: a plan that cut at least one column carries `dateRanges: "cut"` at the top of
`plan.json`, which the plan digest covers. A plan without the key (one written before the cut, or one that cut
no column) replays with no cut at all, so its rows, its run digest and `replay --assert-digest` keep what they
were, and a range that holds none of the column's dates is no refusal there. Any other value is `FLX-2102`.
A plan that cut no column keeps its bytes, its digest and its run id.

**Nothing inside the range is refused (`FLX-3403`)**: the contract's range holds none of the dates the
statistics give the column, or none of the bins that carry weight. So is a cut of the bins of a column that
another column is drawn `given`, since that column's weight tables name the bins (ADR 0036 decision 3) and
dropping or moving one would leave them naming a bin that cannot be drawn. The message names the column and
the range and says what to change. `plan` prints one note for each cut column; it is in no plan document,
digest or receipt.

**What it does not cut.** A column whose value another construct sets is not drawn from its statistics, and
is not cut here: a history's validity start and end, the cells an events or a periods entry sets (its
`op`, `at`, `date`, type, period and override columns, before and after images and the columns that name a
column of the history), every column of a snapshot, a key or a column a relationship sets, and a column a rule
(a `lifetimes`, `within` or `rates` date, a `derive`) sets on every row. Such a column is never `FLX-3403`,
whatever its statistics: a refusal would block a bundle whose layout writes inside the range. `plan` prints
one note for it, naming what sets it (`note: vehicle_month.month is not drawn inside the contract's range
... : its periods entry sets the column, and verify counts the rows outside the range`), and `verify` still
counts its rows against the range.

**A scale above 1 widens key columns past their range.** `--scale` above 1 grows an integer or decimal key
column whose fidelity declares a range (`stages/scaling.py::widened_columns`), so its keys run past a contract
range declared on it. At a scale above 1 `verify` does not count the contract's range of a column the scale
widened, and `plan` prints one note for it (`note: depot.depot_id's contract range (>= 1 and <= 50) is not
checked at --scale 2: the scale widens the column's keys`). A key column the scale does not widen, a column
that is no key, and a scale of 1 or below keep the check.

**`verify` checks the range.** For each emit of the run, the rows of a DATE, TIMESTAMP, INT or DECIMAL column
whose contract declares a range are counted against it (NULL is never outside; numbers read the range as
`author/ranges.py` does; a strict end of any column is strict). A column with rows outside adds one line under
`rows:` and the run fails; `<range>` names the ends the contract declares, each `>=`, `>`, `<=` or `<` as its
rule has it (`>= 2001-01-01 and < 2006-01-01`, or `>= 2024-01-01` alone), while the `plan` note names the
inclusive ends drawn (2001-01-01 to 2005-12-31):

```
  ranges: <expose>.<column>: <n> of <rows> rows are outside the declared range (<range>)
```

and the JSON envelope's `rangeConformance` holds `{"ok": false, "problems": [...]}`. A run whose contract
declares no range, or whose rows all lie inside, prints and returns what it did before: the line and the key
are present only when rows lie outside. A bundle read after the run, whose contract is not the run's, adds
nothing.

## Amendment of 2026-11-25: a history and a periods entry keep to the ranges of the columns they set

The amendment above leaves a column another construct sets uncut and notes it, so a history whose validity
columns declare a range wrote versions outside it, and `verify` failed the run it had planned. The setters
that can keep to a range now do, on kernel `0.2.0`:

- **A history.** The range is the intersection of the ranges declared on the history's validity start and end
  columns and on every projected column that copies a version's start or end: a column of a snapshot, an
  events or a periodic emit named as the history's start or end column, an events entry's `at`, and its
  `date` when the entry has no `lag` (a lag moves the date past the change it records). It is one bound every
  entity shares, read as a parent's lifetime is (ADR 0017, the amendment of 2026-11-24): each first start lies
  at or after its low end, and each chain ends by the day after its high end, so every start and every closed
  end lies inside it. Each entity takes the room for its versions where the range leaves it, else the room for
  one; the counts are held to the room and passed on as under a parent's lifetime, and the rows no entity has
  room for are a shortfall the plan records (`declaredRows`, `shortfall`) and `plan` notes. Unlike a parent
  that ended, the range closes no version: an open last version stays open. The per-row path cuts the first
  start the same way.
- **A periods entry.** A period is kept when its first day lies in the range of the period column; the
  periods keep their anchor, and the ones before the low end or after the high end are dropped.
- **What is refused (`FLX-3403`).** Only a range that does not meet the window: one that leaves no unit between
  `timeBounds.start` and `until` for a history, or holds the first day of none of the entry's periods. A range
  that leaves no entity room for one version is refused as a parent's lifetime is.
- **What is noted.** A column whose setter keeps to its range is not noted. A history's end column whose
  `openEnd` lies outside its range is still noted, since the open end is written as declared (ADR 0017, the
  amendment of 2026-11-25), as are a lagged events `date` and every other column of the list above.
- **What moves.** A range that cuts nothing changes nothing. A plan in which a range cut a history or a periods
  entry carries `rangeSetters: "held"` at the top of `plan.json`, and its history pick (`validityRange`) or
  projection (`periodRange`) says where. A recorded plan without the key replays as it was made, with no
  setter held; any other value is `FLX-2102`.

## Amendment of 2026-11-26: an events entry with a `when` holds no history to a range

The amendment above holds a history to the range on an events entry's `at`, and on its `date` without a `lag`,
as if the entry recorded every version. An entry with a `when` records only the versions its condition holds for,
so the range on its columns says nothing of the others: read as a bound on every version, it moved every entity's
first start to the range's low end, and under a parent's lifetime (ADR 0017, the amendment of 2026-11-24) the
entities of a parent that closed before that day had no room, which `plan` refused (`FLX-3403`).

- **What holds.** An events entry with a `when` adds no column to the history's range, and its columns are noted,
  as before the amendment above; `verify` counts the events outside the range. A `when` that keeps the events
  inside the range, such as one on the version's start, leaves no such row.
- **What is refused.** A range on the history's own validity columns, or on an entry without a `when`, is still
  a bound every entity shares. Where it leaves an entity whose parent closed before the range's low end no room for
  one version, `FLX-3403` names the range's first day beside the parents' lifetimes and says to widen the range.
- **What moves.** A bundle whose only range on a history was such an entry's no longer records `rangeSetters`, and
  its versions keep the first starts they had before the amendment above. No released version held such a range.


## Amendment of 2026-11-26: a snapshot whose history sets both dates takes no lifetime

`init` reads a table's date pair as a lifetime unless a history, or an events or periods entry of one, sets either
column. A `snapshots` entry was not counted: its emit takes every column it declares from the history's last
version, so a lifetime over two of them was a second source for both dates. The plan kept it as a check the rows
could not be drawn to, and `verify` failed it on the share of entities whose history did not end the way the
profile's durations say.

- **What is dropped.** A date pair of an emit `init` makes the history's snapshot, when both its columns are
  columns the snapshot takes from the history and no rule of the contract sets either. The pair's statistics are
  unused, for the reason "the history sets both dates".
- **What is kept.** A pair one of whose columns a derive, a dependency, a copy or a key sets, and every pair of an
  emit that is not a snapshot.
- **What moves.** A bundle `init` writes for such a snapshot. No plan or recorded plan changes.

## Amendment of 2026-11-26: a check orders two dates

### Context

A contract can order two dates of one row as an invariant, such as `cancelled_on IS NULL OR
cancelled_on >= issued_on` at most 5 % failing. `init` wrote an order only from a profile's
`datePairs`. A private release publishes none, so each date was drawn on its own, and the check held on
a share of the rows well below its tolerance.

### Decision

1. **What is read.** A `check` of the contract, with no `when`, whose syntax tree (the engine's own
   `flux-expr/2` parser, never a pattern over the text) is `b >= a`, `b > a`, `a <= b` or `a < b` over two
   columns of the row, optionally behind `b IS NULL OR` or `a IS NULL OR`. Anything else orders nothing here.
2. **Which form.** A lifetime. It is the one form that orders two dates of one row: a `within` entry orders a
   child's date inside a linked parent's lifetime, and a `derive` would fix the gap, so the end could not
   draw the days between the dates as an open question. The lifetime starts at `a` and ends at `b`.
3. **The gap is an assumption.** With no published pair, the days between the dates are drawn evenly from
   0 days (1 for a strict order) up to the days between the window's start and end, the most a row that
   starts at the window's start can hold. Evenly says the least: nothing published favours a short or a long
   gap, and a gap longer than the window only ends after it. The number is assumed in the register, naming
   the check, so a person can replace it. No `target` is written: every row holds the order.
4. **The end.** A nullable end the check lets be NULL keeps the lifetime's default, so an end after the
   window is NULL (a card not cancelled by the window's end). A nullable end the check does not let be NULL,
   and a required end, keep their date past the window (`endPastWindow: kept`); then the bundle is written
   only when the end's declared range holds every end the gap can draw (`FLX-6204` would refuse it
   otherwise), and no lifetime is written when it cannot be shown to.
5. **What wins.** A published `datePairs` entry for the two columns, used or not, is read exactly as before
   and this reads nothing. A table declares one lifetime: a pair's, when it has one.
6. **Prior art.** The same construction a generator uses to keep a return date after a purchase date: the
   later date is the earlier one plus a random offset (dbldatagen's date ranges), rather than sampling both
   and rejecting rows, which loses the tolerance the check allows. A column inequality constraint that
   rejects and resamples is the other common form; SDV's Inequality constraint was looked at for the
   pattern only and nothing of it was taken.
7. **A check not written is named.** When `init` cannot write a check as an order (a column a rule or a
   history sets, two dates a snapshot's history sets together, a window too short to draw a gap in, an end range
   that cannot be shown to hold the gap), its report carries a line naming the check and why.
8. **When the lifetime is written.** The lifetime replaces the end's own histogram, so it is written only
   where the histograms do not already keep the order within the check's tolerance.
   - *No usable distribution.* When the end, or the start, is drawn evenly within the window (the profile
     published no order statistic, or no bin stands clear of the noise) or from no histogram of its own, there is
     nothing to estimate from and the lifetime is written.
   - *The estimate.* Otherwise `init` estimates from the two histograms the bundle draws the dates from the share
     of rows that would break the order if the two were drawn independently: the end before the start (on or
     before it, for a strict check), each bin's mass spread evenly over the days it covers (the milliseconds, for
     a timestamp), in exact fractions, so the same inputs give the same number. The generator draws the last
     declared bin closed at its top, so that bin also holds its upper edge (one more day, or millisecond). A NULL
     date counts as breaking the check where the check does not hold on a NULL one.
   - *The tolerance.* The check's tolerance is the share its band allows, read from `mustBeLessThan`,
     `mustBeLessOrEqualTo` or `mustBeBetween` from zero, in `percent`. A check with no such band, or a band in
     `rows`, has none, which counts as 0.
   - *The margin.* A run draws its rows by chance, and a share just under the tolerance falls outside it about
     half the time. The estimate `p` is kept only where `p` plus three standard errors, `sqrt(p (1 - p) / n)` at
     the `n` rows the table plans, is at most the tolerance (below it, for `mustBeLessThan`); it is compared in
     exact fractions, as `9 p (1 - p)` against `n` times the square of the room left. A table whose rows follow a
     history plans none, and compares `p` itself.
   - *Within it.* When the estimate keeps that margin, both histograms stay and no lifetime is written. The
     notes name the check and say the published dates already keep it within its tolerance, with the estimate.
   - *Above it.* Otherwise the lifetime is written as above and the end's histogram is traded for the order. The
     notes name the check and say so, with the share that independent dates would break.
   - *A kept check is not undone.* A check kept this way rests on the histograms of both its dates. Every check
     of a table is decided before any lifetime is written, taken in the order of the checks' ids, so the order
     the contract lists them in decides nothing. A lifetime is then not written that would end on a date a kept
     check rests on (a table declares one lifetime in any case): the other check is not written as an order, and
     its note names the kept check, which holds through its published histogram. A date that a lifetime from the
     profile's published date pair ends is laid out by that pair, so no check rests on its histogram: a check of
     that date is not written, and its note says the pair lays it out.

   The two choices cost something. A kept pair of histograms holds the order as far as a sample of rows follows
   them, and a small table can fall outside the tolerance by chance; the lifetime holds every row. A lifetime
   means the end no longer follows a histogram of its own that the profile publishes, and a fidelity check on that
   histogram can then fail where the check on the order now holds. Publishing the date pair for the two columns
   gives the gap a distribution and ends the trade.

## Amendment of 2026-11-26: `init` widens a date that bins alone declare

A date column whose profile publishes quantile bins and no minimum is declared by its bins and has no `range`; the
plan reads the first and last edges as its range (`declared_bounds`). The widening that `init` writes for an enforced
`within` date moved only a declared `range`, so it skipped such a column, the plan asked for the same span again and
`init` ended in `FLX-6204`.

- **The range the plan reads.** The widening takes that range for the column, moves its ends as it moves a declared
  one, writes the result as the column's `range` and takes the outer bin edges that sat on a moved end along, as it
  does for a column that declares both. A histogram property whose edge sat on a moved end is left out with the same
  reason.
- **The contract.** The check that keeps the widening inside the range the contract declares reads the same range, so
  a bins-only date is not widened past it, or past the days a derive over the column keeps in range, and its entry is
  left out as for a declared range.
- **What does not move.** A bins-only column no entry asks to widen declares no `range`, and keeps its bytes.



## Amendment of 2026-11-26: `init` declares the contract's range on an order lifetime's end

The generator cuts a lifetime's gap to the end column's declared range `Re` (section 4), but a column the profile gives
no distribution declares none, so no `Re` existed and nothing was cut: with a window much wider than the data, a gap
drawn evenly over the window put most ends outside the range the contract holds the column to.

- **The range.** When `init` writes a lifetime for a check that orders two dates, it declares on the end column the range
  the contract's rules give the column, when both ends are given (a strict end is moved one day, or one millisecond,
  inward). The cut then applies and every end lies inside it. A column the bundle declares no bins, levels or range for
  takes the contract's range. A column whose bins or range reach past the contract's range takes the part inside it
  (the intersection), and a published release often rounds its extremes outward, so this is the usual case for an end.
  The end's draw is the lifetime, so the column's bins and their weights are left out, and so is any histogram property
  of the column: the bins of a histogram partition the declared range (`FLX-5032`), and a draw's bins stay inside it
  (`FLX-3402`), so none can hold once the range is cut. The notes say that the histogram was traded for the order, and
  the statistic it was read from is recorded as used. A table left with no property to verify declares none.
- **The gap.** The longest gap is at most the range's last instant less the earliest start the bundle can draw (the
  start's declared span, else its contract range within the window, else the window's start). A range that leaves no gap
  after the earliest start writes no lifetime, and the notes say so.
- **What it admits.** The two conditions of `FLX-6204` are checked against the range written, so a required end, or a
  check with no NULL branch, over such a column is ordered where `init` declined it before.
- **What does not move.** An end whose declared span lies inside the contract's range keeps its range, its gap and its
  bytes, as does an end with no closed contract range, and the nullable end that a check lets be NULL keeps the default
  `endPastWindow`. The cut to the declared range leaves a nullable end open only where the cut is empty, that is, where
  no value of the end fits the range on a row (section 4); every other end lies inside the range. An end whose published
  bins lie wholly outside the contract's range is refused as before (`FLX-3403`).
  Deriving the gap from the two published histograms by deconvolution is a later improvement.

## Amendment of 2026-11-27: the validity window takes a table's lifetime from a pair that lays out one of its dates

A table declares one lifetime. A versioned table whose history `init` does not write (a required end with no declared
open end, or no history entry in the release) has the window its contract declares, from a start to an end, and every
version ends on or after its start. The lifetime a published date pair gave a table could lay the window's end out from
a column outside the window, and the start, drawn on its own, was then ordered with it only by chance.

- **When the window takes the lifetime.** The best pair of the profile is the pair of a window date and a column
  outside the window. It takes the lifetime from the window where it lays out the window's end, since the end then
  follows a column the start does not. Where it lays out the window's start, or a column outside the window from the
  start, the window takes it only if the window's two histograms, drawn apart, would break its order (the check's
  tolerance where the contract declares one, none otherwise); a window they keep in order is left alone.
- **What the lifetime is.** The window's own pair where the profile publishes one that holds on enough rows (section 5
  applies to it as it does to any pair). Otherwise a lifetime from the window's start to its end with the gaps derived
  from the two published histograms (the amendment of 2026-11-26 to ADR 0017, `author/gap_fit.py`), or drawn evenly
  from one day to the days of the run's window where either is not published. Each is an assumption in the register, and
  the end keeps its date past the window where it is required (`endPastWindow: kept`) and is NULL there where it is
  nullable. For a required end, or a nullable end that keeps its date, no lifetime is written, and the best pair stands,
  where the end's declared range cannot be shown to hold every end the gaps can draw; a nullable end that is NULL past
  the run needs no such proof, since no end the gaps draw falls outside the range.
- **What is unused.** Every pair of the profile that was a candidate for the lifetime, with a reason that names the
  window (the history, where the release's history entry already gave the window its lifetime; a window pair that
  publishes no days says so). A column the other pair tied to a window date (published on the same day, say) is drawn on its own: a table
  declares one lifetime, and the order the contract states for its window is the one kept.
- **The end's range.** The window's lifetime is held to the range the contract gives its end as a lifetime written for a
  check is (the amendment above): the gap is cut to it, and where the published histogram of the end reaches past it the
  range is declared on the end and the histogram is left out. A history entry's lifetime is read against the spans the
  bundle declares, as before.
- **Used and unused.** A pair whose order decides that the window takes the lifetime is used, with a reason that holds no
  value; so is a pair that shows a column on the history's start day, which keeps the history out (ADR 0017). Only a
  statistic no decision reads is listed unused, so dropping the listed statistics never changes the bundle.
- **A pair the release withholds.** A pair of the window's two dates whose order the release withholds gives no
  lifetime; with no other pair to take it, the window takes it the same way.
- **What does not move.** A table whose history is written, a release whose `history` entry names the window (its
  lifetime was already written from the entry), a window whose two histograms keep its order within the check's
  tolerance, a table with no declared window, and a window the profile publishes the pair of as the best pair, keep their
  bytes.

## Amendment of 2026-11-27: a history keeps the first start with a date the contract orders before a version's start or end

A contract can order two dates of the table that holds the current row of each entity (the snapshot of a history), or
of the history itself: `ended_on IS NULL OR ended_on >= began_on`. When the later date is the start or the end of a
version (a validity column, or a column a `derive` rule takes only from those and NULL, as
`CASE WHEN status = 'ENDED' THEN valid_from END`), the history sets it, and so a snapshot's two dates are both the
history's and take no lifetime (the amendment of 2026-11-26). The earlier date was left to its own histogram and to
the `within` entry that lays it in its parent's life, drawn apart from the starts, and the check held on fewer rows
than it declares. A release that withholds the pair's order, or lists no pair, gave `init` nothing to read the order
from, and the check was not read either, since the pair was listed.

- **What is read.** A `check` of the contract of the history's table, or of a table without a validity window whose
  primary entity is the history's natural one, that is not strict, whose later date is a validity column or is set by
  a `derive` (with no `when`) whose result is only validity columns and NULL, and whose earlier date is a plain column
  of the window's type that a key, a rule, a dependency or a copy does not set. The profile publishes no readable order
  for the two dates of that table: no pair, or a pair whose order is withheld, suppressed or noisy. A pair whose order
  is published lays the dates out already, as the amendment of 2026-11-26 says, and nothing is read.
- **What is written.** The history entry's `activation` is the earlier date (ADR 0017 section 5): every version holds
  the entity's first start there, and every start and end of a version is on or after it, so the check holds on every
  row. The first check by rule id, then column, is taken, and the register names it; when another check qualifies for
  a different column, the register names that check as left unkept. A contract that declares its own
  `lifecycle.activation` keeps it, and that member is the only one read. An activation read from a check is not one the
  contract declared: it does not lift the refusal below.
- **What is declined.** When the earlier date may be NULL (the contract does not require it and the profile publishes a
  count of NULLs that is not zero), an activation would drop those NULLs, so none is written and the column keeps its own
  draw. The register says so, with no value in it.
- **The tie refusal stays.** A history is not written when the release publishes a column on the start's day and the
  lengths are withheld, unless the contract declares `lifecycle.activation` as that column. The activation read from a
  check does not lift that refusal: a history refused for the tie keeps its bytes.
- **What does not move.** A release that publishes the order, a strict check, a check whose later date no version sets,
  a column a rule or a key sets, a contract that declares its own `lifecycle.activation`, a history refused for the tie
  and a contract with no such check keep their bytes. No generator or plan code changes.

### Only a readable order claims a pair

The same reading holds for the lifetime of a contract's check. A pair that a table lists claims its two dates for the
profile (and the check writes no lifetime of its own) only when the pair's order is published and readable. A pair
whose order is withheld, suppressed or noisy is listed and orders nothing, so the check of the two dates is still an
order to keep and writes its lifetime, as it does where the release lists no pair. The history's activation above and
this share one test of whether a pair has a readable order. A release with such a pair gives a bundle that carries the
check's lifetime where it carried none; a pair with a readable order keeps its bytes.

A pair that gives no usable duration writes a lifetime with its start only, and its order named that start, so the
order is read and is not listed as unused.
