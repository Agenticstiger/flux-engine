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
