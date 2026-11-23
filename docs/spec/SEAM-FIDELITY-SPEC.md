# Seam generator and fidelity check: normative specification (v0.1)

## 1. The contract-schema generator (`generator/`)

Input: a resolved expose (`seam/contract.py: compile_expose`) → ordered columns with `LogicalType`. For each column
the generator picks a **family** from the column's declared aggregate statistics (§3) or, absent statistics, from
the type default:

| Column type (6) | Default family | Families allowed (4) |
|---|---|---|
| STRING categorical (declared support or ≤ 64 distinct) | categorical | categorical, empirical-histogram |
| INT | poisson/exponential-rate (counts) or uniform (ids) | categorical, bounded-normal, rate, uniform/empirical |
| DECIMAL(p,s) | bounded-normal | bounded-normal, rate, uniform/empirical, categorical |
| BOOL | categorical (Bernoulli) | categorical |
| DATE / TIMESTAMP | uniform within `timeBounds` or rate (inter-arrival) | uniform/empirical, rate |
| identifier (STRING/uuid with `unique`, or a one-column STRING key the contract declares: KERNEL-SPEC section 8.1) | derived deterministically from `(H, ordinal)`, never random | none |

Every draw uses the §4 kernel samplers on the path `("sim", sim_id, "gen", exposeId, "row", rowIndex, column)`.
A column declared `given` another (§2) keeps that path: what it reads from the other column is the level that
column drew in the same row, which picks the weights it draws with.

**Keys and foreign keys.** The columns a FLUID 0.7.7 contract makes keys or foreign keys are produced as
KERNEL-SPEC section 8.2 says, so the exposes of one contract are generated together: a key is laid out from the
row index and never repeats, and a foreign key takes its parent's key at a parent row. A foreign key that declares
a draw draws on its own path as above and holds the parent's nearest key; one that does not picks its parent on the
stream of its relationship's first column. Such a column needs no declaration of its own. A one-column key may
declare only a range, or bins with `binWeights`; a column of a several-column key, a range, bins or levels.
Rows are produced in `rowIndex` order; the number of rows is declared (`extensions["com.agentics.flux/1"].emits[<exposeId>].rows`,
§2; default = population size when the emit is scenario-driven).

**Two row sources, and the declaration picks.** The generator above runs for an emit whose extension block
declares a `fidelity` block: declared statistics decide every value, and the fidelity check is specified over
them. An emit that declares no block is **scenario-driven**: its rows are the run's own moments, and one detector
firing for one subject is one row.

The scenario path (`seam/mapping.py`, `stages/pipeline.py`):

* **the moment is the detector fire**, not the journey transition. A transition exists only where a Journey's
  trigger held and a persona reaction was drawn, so journey-driven rows would silently drop every burst the
  detector found outside a journey's reach; the detection also carries its matched events, so every mapped column
  has a source.
* **the mapping is resolved at plan time**, once per emit, by three steps in fixed order, first match wins:
  (1) the extension block's `mapping` override; (2) the vertical's alias table (`verticals/<pack>.py`, e.g.
  `subscriber_id → subject.id`, `aborted_at → event.time`, `amount → event.data.amount`, `market →
  subject.market`); (3) the column's own name, searched under `event.data.`, `subject.`, `subject.traits.` in
  that order. A **required** column no step resolves is `FLX-3302`, naming the column, before any row exists; an
  optional one renders JSON `null`.
* **an INT column's source is checked in the same step**: it must be integral (KERNEL-SPEC §9), read off the
  declared families and never off the values a run drew, or the emit is `FLX-3303`, naming the column and the
  source, before any row exists. A `Decimal` from an integral source reaches the renderer as the exact `int` it
  equals.
* the moment's event is the **last** matched event of the detection: the one that completed the pattern.
* rows are scheduled through KERNEL-SPEC §7's scheduler at `phase = EMIT`, keyed on that event's instant, and
  written through the same `EmitWriter`; `outputDigest` is unchanged (§8 of KERNEL-SPEC, computed above the sink).
* a declared `rows` **caps** a scenario-driven stream; a run with fewer moments emits what it has and never pads.

**Coverage.** Whichever way an emit's rows are produced, every column has one producer, and the plan says which:
`declared` (its entry under `fidelity.columns` decides the draw, or, for a foreign key, decides the value it then
moves to the parent's nearest key), `copied` (a runtime attribute, by one of the three steps above), `derived` (an
identifier, from `(H, ordinal)`; a key the contract declares, laid out from the row index; a foreign key whose
parent row its relationship picks), `profile` (a column of a reference expose, whose value the pinned profile publishes for its row, decided by `reference`) or `default`. A column no declaration reaches takes its type's default and is a `default`: an optional column the
mapping resolves to nothing (`null`), or a DATE or TIMESTAMP column that declares no draw of its own (§1's table:
uniform within `timeBounds`). A `default` is named by `plan`, recorded by `apply` in `column-provenance.json` and
listed under "What Was Not Proven" in `EVIDENCE.md`. Under `--strict-coverage` it is `FLX-3304`, naming every such
column of every emit, before any row exists. Strict coverage also refuses, with `FLX-3305`, a declaration the
contract makes and the run does not generate (`sharedColumns` or `sharedAttribute` on kernel 0.1.0, in a Simulation that does not declare `copies: true`, or on a key or foreign
key column, a check that determines no column, a check over a link), which `plan` otherwise names. `plan.json` records the digest of the record as
`columnProvenanceDigest` (the `raw-bytes` digest of the file, as `seedTreeDigest` is of `seed-paths.ndjson`), so
`planDigest`, the run id and the run digest cover it. `apply` refuses to write a record the plan does not name, and
`ship` and `verify` read the record only through that digest and refuse a missing or moved one with `FLX-2115`; a plan
without the key binds no record, and the report says so. The
setting changes whether a run is accepted and never what an accepted run produces: the same bundle and seed give the
same `plan.json`, `runDigest` and rows with `--strict-coverage` on or off, at any `--jobs`.

**Strict coverage in the bundle.** A Simulation can ask for the same refusal itself: `strictCoverage: true` at the top
level of the namespace (§2). The run is strict when the flag or the declaration asks, and the flag cannot turn a
declaration off, because the declaration is part of the bundle the run's `inputDigest` covers and a run that could
switch it off would not be the run the bundle declares. The refusal names which one asked, so the way out is plain:
drop the flag, remove the key, or both. The value is a boolean and nothing else (`FLX-3105`); `false` and a null declare
nothing. The declaration is a bundle edit, so it moves `inputDigest` and with it the run id, but `plan.json` has the
same keys with it as without (the record's `columnProvenanceDigest` included), and a run it accepts makes the rows and
every component digest it makes without it.

**A column's sensitivity sets the minimum privacy grade of the run (ADR 0020).** The contract may label a column with
a FLUID sensitivity, `exposes[].contract.schema[].sensitivity`. A run has one grade, read from the profile its Simulation
pins and from nothing the bundle says about itself: `P0` when it pins none (every parameter is declared by hand, and the
engine read no data), `P3` when the profile states a release under differential privacy, declares its structure public
and holds only noisy statistics (PROFILE-SPEC.md section 3a), and `P1` otherwise. `pii`, `phi`, `restricted`,
`pseudonymized` and `tokenized` require `P3` or stronger (`P0` meets it too: no data was read); `confidential` and
`internal` require `P1` or stronger; every other label allows any pattern. Each labelled column's minimum is compared with
the run's grade. `plan` refuses a run with a column below its minimum, `FLX-4005`, in the governance gate that also refuses
a zone outside the allowed ones (`FLX-4001`), before any row is drawn and before anything is written, and a plan that pins
a profile records its grade as `privacyGrade`. `apply` (a fresh plan, `apply --run-id` and so `replay`) and `ship` enforce
the minimum again, `apply` from the recorded grade (a plan that pins a profile and records none is read as `P1`) and `ship`
from the profile it is handed. A Simulation can raise a floor under `privacy.minimumGrade` at the top level of the
namespace, as `privacy: { minimumGrade: { confidential: P3 } }`, and a value below a label's built-in floor, a label FLUID
does not define, and the unassigned `P2` are `FLX-3105`. The key is a bundle edit and moves `inputDigest` and with it the run
id; the same bundle that plans makes the rows and every component digest it makes without the key. A grade for each column
is a later refinement.

## 2. The fidelity block (lives in `Simulation.extensions["com.agentics.flux/1"].emits[<exposeId>].fidelity`)

`fidelity` is the block's **only** spelling in a document. The
subsystem keeps its own module name (`flux_engine.concordance`), and that name
stays inside the package: a second accepted key would be two digests of one
contract (the same reason §2 refuses a bare support list below) and it would
put a reserved word into a file an author writes.

**Why it does not sit in `spec.emits[]`.** FLUX 0.5.0 declares `Simulation.spec.emits[]` items with
`additionalProperties: false`, so a bundle carrying `fidelity` or `rows` there is refused by the published
validator and by the packer; the bundle cannot ship. RFC-02's document-level `extensions` map is the one
sanctioned extension point (`propertyNames` = `reverse-dns/major`, contents owned by the namespace), so this
engine's declarations live there and every bundle stays valid against the **unmodified** 0.5.0 schema.

The namespace carries five keys and no others: `emits`, keyed by `exposeId` as below; `profile`, which pins a
profile document by its digest (PROFILE-SPEC.md §6); `scorecard`, which declares thresholds on the scores that
profile gives the run (SCORECARD-SPEC.md §4); `parameters`, the parameter register, which says what each number the
run reads rests on (PARAMETER-REGISTER-SPEC.md); and `strictCoverage`, a boolean (§1). Any other key is refused
(`FLX-3105`). The keys the later records add, `facts`, `links`, `bridges`, `counts`, `copies`, `generators`, `overlay`, `history`, `asOf`, `snapshots`, `events`, `periods`, `lifetimes`, `within`, `rates`, `privacy` and `unused`, are described where
they apply (§3 for the fifteen that change a run's rows, its tolerances, or the profile it is scored against). A new block starts at
the top level of the namespace when what it declares belongs to the Simulation rather than to one expose.
An emit's block carries eight keys and no others: `rows`, `mapping`, `fidelity`, `invariants`, `rules`, `reference`, `hidden` and `hiddenColumns`. `invariants`
holds FLUX settings for the rules of the emit's contract (`exposes[].contract.invariants[]`), each keyed by the
invariant's `id` (EXPR-SPEC-2.md §1, §6). An invariant's settings carry `generates`, the column a bounds or difference
form generates. Step 3 of ADR 0011 adds two more keys and no others: `distribution`, the draw of a bounds form relative
to a parent value (EXPR-SPEC-2.md §6, rules over a relationship), and `target`, the percent of covered rows that
violate (EXPR-SPEC-2.md §7). The setting for a rule's violations that EXPR-SPEC-2.md §7 names comes with a later
step; until then a violation's candidates are drawn from the column's own statistics. `rules` holds the emit's
FLUX-side rules, a list of entries that each name their `language` (EXPR-SPEC-2.md §1).

`reference: true` makes the emit a reference expose: its rows are the rows the pinned profile publishes for it
(`references`, PROFILE-SPEC.md §5b), one per published row in published order, each cell read as its contract
column's type (a number as canonical decimal text, a date as `YYYY-MM-DD`, a timestamp as its milliseconds, a bool
as `true` or `false`, a string as stored, `null` as NULL). The block carries no other key, the Simulation pins a
profile, and that profile's `references` holds the expose; each of these, and kernel 0.1.0, is `FLX-3105`. Every
contract column is one of the published columns and every published column a contract column, every cell fits its
column, every key the contract declares is unique and never NULL, and a relationship the expose declares points at
another reference expose that holds every non-NULL key it uses; each is `FLX-3403`. A reference expose draws
nothing, so its bytes do not depend on the seed, and its plan entry names the layout, the row count and a digest of
the published values, not the values. Because it is released whole, a column of it that carries a FLUID
`sensitivity` which sets a minimum grade is refused (`FLX-4005`), and `EVIDENCE.md` lists the expose as released
outside the run's guarantee. `verify` checks that its output equals the published rows, as a line of its rules.
Kernel 0.2.0 only (KERNEL-SPEC.md §8.2).

**A hidden column.** An emit's `hiddenColumns` lists `{name, type}` entries: a column the expose's contract does not
declare, of a FLUID logical type written as the contract writes one. A hidden column is planned as a contract column is,
after the contract's columns and in the order listed: it has statistics in the emit's `fidelity` block, a rule may set
it, a path may read it, and a lifetime may name it. It is drawn on the generator's cell path with its own name (§1), a
path no existing column draws on, because a hidden name that is a contract column's is refused (`FLX-3105`). It is never
written: it is in no stream, no header, no `outputDigest` and no sink. Kernel 0.2.0 only, and kernel 0.1.0 refuses the
key (`FLX-3105`, naming the kernel).

**A hidden table.** An emit's `hidden: true` makes the emit a hidden table. It is planned and generated as any emit, its
rows are what a path through it reads, and it is never written: it has no stream, no file and no `outputDigest`, and its
plan entry says it is hidden. A relationship into it, and so a lifetime that rests on it, is generated as a relationship
into a written expose is. A hidden table that no written emit reads through a link is refused (`FLX-3105`), since nothing
would use its rows. Kernel 0.2.0 only, and kernel 0.1.0 refuses the key (`FLX-3105`, naming the kernel).

**Lifetimes.** The namespace key `lifetimes` lists `{emit, start, end, duration, endPastWindow, target}` entries, at most
one per emit. `start` names a DATE or TIMESTAMP column of the emit, and `end` optionally another of the same kind, either
one hidden or not. The row's lifetime is the closed interval from its start to its end, its last day included, and it is
open when the end is NULL or when the entry names no end. Its fact is that the end is not before the start. A lifetime
with a start and no end is how a row that only begins is declared, such as an invoice that a payment follows. A row whose
end is before its start violates its own lifetime, which its `lifetime:` line counts once; for every row that reads it as
a parent, its lifetime is open. A malformed entry, an unknown key, a second entry for one emit, and kernel 0.1.0 are
`FLX-3105`.

With `duration` the end is drawn as the start plus a duration in whole days: `{bins, binWeights, closed, open}`, a
histogram as EXPR-SPEC-2 §6 writes a `distribution`, over the days from the start to the end, and `open`, an optional
weight beside the bins for the rows whose lifetime does not end. Its first edge is `0` or later (`FLX-3105`), since an
end drawn before its start would break the lifetime on a row the generator made. `endPastWindow` says what an end after the run's window
(`spec.timeBounds.end`) is: `open`, the end is NULL, because the window never saw it; or `kept`, the end keeps the drawn
date, as a scheduled end such as a subscription's validity has. It defaults to `open` for an end column the contract
leaves nullable and to `kept` for one it does not. `open` on a column the contract requires is `FLX-3105`, and so is an
`open` weight on a `kept` end. Without `duration`, the end is drawn from its own statistics and the fact is checked, not
generated.

The end stays inside its column's own range. With `Re` the range the end column's declared statistics give it
(`range: {min, max}`; for a `marginalHistogram` its first and last edges, the support `FLX-5005` checks a written value
against), the duration is cut so that the start plus the duration stays inside `Re`: the day interval
`[Re.min - start, Re.max - start]` meets `[0, inf)`, and the histogram is cut to it at the end column's grain by the same
cut arithmetic as a `within` offset (a bin keeps the part of its weight its values inside the interval hold, and the
`open` weight stays whole), still one `uniform53` at position 1. An exponential is cut exactly: the least and the greatest
`uniform53` whose exponential value lies in the interval bound a span, and `u` is mapped onto it. A column whose statistics
bound it has no `Re` and is drawn as before. Where no value fits on a row, a nullable end under `endPastWindow: open` is
NULL, so the lifetime is open; anywhere else `plan` refuses the entry (`FLX-6204`) whenever `s_max + d_min > Re.max`, and,
as the mirror, whenever `s_min + d_max < Re.min`, with `s_min` and `s_max` the start column's declared bounds and `d_min`
and `d_max` the shortest and longest duration the entry can draw (an exponential has no longest), so a run never meets such
a row.

A `duration` has one of three forms, told apart by their keys, and a mix of two or an unknown key is `FLX-3105`. The
histogram above has `bins`, `binWeights`, `closed` and `open`. An exponential is `{mean, open}`: `mean` is the mean
duration in days, a decimal string above 0, and `open`, optional, is the share of rows with no end, a decimal string from
0 up to below 1. Both are held to the digits of a weight (`FLX-3105`). A dwell family is `{given, states, otherwise}`:
`given` names a column of the emit, hidden or not, other than the lifetime's start and end; `states` maps the text of a
value of `given` to a histogram or an exponential, with at most 1 024 states and no empty text; and `otherwise`, optional,
is the dwell of a row whose state is NULL or not listed. A family holds at least one of `states` and `otherwise`. The state
of a row is its final value of `given`, written as EXPR-SPEC-2 §8 writes the text of a value, so the end is set after
`given`, and a `given` that the end decides is a cycle (`FLX-6208`). Without `otherwise`, a row whose state has no dwell
keeps the end its own cell drew, as a row whose start is NULL does. An exponential draws one `uniform53` at position 1 of
the end's dates stream, as the histogram does, after the `open` share, as ADR 0025 §3 says; an `open` share on a `kept` end
is `FLX-3105` here too. The new document shapes enter `generatorDigest` only where a bundle declares them.

**A date within a linked row's lifetime.** The namespace key `within` lists
`{emit, relationship, date, lifetime, offset, target, exposure}` entries. `relationship` is a to-one relationship of the emit, or a
chain of them written with dots as a FLUX-side path is (EXPR-SPEC-2 §3), whose last target declares a lifetime. An entry
names exactly one of `date`, a DATE or TIMESTAMP column of the emit, and `lifetime: true`, the emit's own lifetime; any
other entry is malformed (`FLX-3105`).

- The fact for a single date `c`, with `p` the row its link reaches, is `p.start <= c`, and `c <= p.end` unless `p`'s
  lifetime is open. The rows it covers are the rows whose link reaches a row whose start is not NULL; a row whose parent
  start is NULL is not covered and keeps the value its own cell drew.
- A DATE against a TIMESTAMP: a lifetime includes its last day, so a DATE start bounds a TIMESTAMP from its midnight UTC,
  and a DATE end bounds it up to the end of that day, before the next midnight UTC. A TIMESTAMP bounds a DATE by the day
  that holds it.
- The fact for a period, `lifetime: true`: the emit's start is within `p`'s lifetime as above, and when `p` has ended the
  emit's end is not NULL and not after `p.end`.
- The interval. A date is drawn inside `J`, `p`'s lifetime cut at the run's window: the part of the lifetime inside
  `spec.timeBounds` when there is one; otherwise, when the lifetime ended before the window, the whole lifetime;
  otherwise, when it starts after the window, `p.start` alone. So the lifetime always wins over the window, and a date
  lies outside the window only where its parent's whole lifetime does. Since a parent whose end is before its start
  reads as open, `J` is never empty on a covered row.
- The column's own range. A date is drawn inside `J ∩ R`, with `R` the range the date column's own declared statistics
  give it (`range: {min, max}`; for a `marginalHistogram` its first and last edges), in the column's units, so that a
  written value never leaves the support `FLX-5005` checks it against. A column whose statistics bound it has no `R`. The
  cut histogram and the uniform draw below run over `J ∩ R` with the same samplers and positions, and so do the proposals
  of seasonal rates. On a row where `J ∩ R` is empty the date keeps the value the column's own cell drew, which its own
  statistics drew inside `R`, nothing more is drawn, and the row is counted `infeasible` for the entry, so it counts toward
  the fact's violations and `verify` reports it as one.
- The draw. With `offset`, a histogram of `DAYS_BETWEEN(p.start, c)` written as a `distribution` (`bins`, `binWeights`,
  `closed`), the date is the value EXPR-SPEC-2 §6 draws inside `J ∩ R` from that histogram, one `uniform53` at the column's
  grain. Without it, and on a row whose bins leave no value inside `J ∩ R`, the date is uniform over `J ∩ R` at the column's
  grain, one `bounded` at the same position. Which of the two samplers a row takes depends on `J` and the bins, never on a
  draw, and a row that falls back from its offset is traced as an offset fallback.
- Several entries for one column name relationships along one chain, each a prefix of the longest (`account` and
  `account.customer`), and at most one of them carries an `offset`; entries that are not on one chain, and two offsets,
  are `FLX-3105`. Their lifetimes are intersected first and the intersection is cut at the window by the rule above, so
  entries that meet inside the window and entries that meet before it both draw. The lifetimes along one chain fail to
  meet only where a middle row leaves its own parent, which only a `target` on that row's entry allows. So `plan` refuses
  an enforced entry that another entry's target can make unmeetable (`FLX-6204`, as EXPR-SPEC-2 §7 refuses a band that
  zero violations would fail). When both carry a target, a row whose lifetimes do not meet is drawn inside the nearest
  link's interval, counts as a violation of the outer entry, and is traced as infeasible. At most one entry for a column
  carries a target above zero (`FLX-3105` otherwise), since a violating row violates one entry and holds every other. A
  middle expose's tie (its own `within` entry or period through the next relationship) that carries a target lets that
  row leave its parent, so every entry further out on the chain must carry a target too, else `FLX-6204`; with one
  target per column such a chain has two levels.
- An enforced entry (no `target`) for a column that declares a range is refused by `plan` (`FLX-6204`) when the declared
  ranges make an empty `J ∩ R` possible on some row. With `W` the window, `Rs` the declared range of the parent's start
  column and `e_min` the smallest end the parent's lifetime can take (`Rs.min` plus the shortest duration its entry can
  draw, or the parent end column's declared minimum, or no bound for a lifetime with no end), `J ∩ R` is never empty when
  `Rs.max` is at most `R.max`, `e_min` is at least `R.min`, and `R` meets `W`. For several
  entries on one column the check runs on the latest possible start and the earliest possible end; for a period it runs
  on the start so, and on the end against the end column's range whenever a parent's end can fall below the range's
  minimum. The message names both remedies: widen the column's declared range, or give the entry a `target`. An entry with
  a target is not refused; its empty rows are counted `infeasible`.
- With several entries for one date along a chain, the first middle expose must declare a `within` entry for its
  lifetime's start or a period (`lifetime: true`) through the next relationship of the chain, and every deeper middle
  expose a period, or the plan refuses it (`FLX-6204`): without the tie the lifetimes of the levels need not meet. Periods
  generate at every level.
- A run with no `spec.timeBounds` refuses a date whose entries declare no `offset`, or an `offset` that puts no positive
  weight on a day from 0 up (`FLX-6205`, as EXPR-SPEC-2 §6 refuses an interval open on one side): a parent whose
  lifetime has no end would leave `J` with no upper end.
- A period. For `lifetime: true` the start is drawn as above, and the end is drawn as the lifetime says, then cut at
  `p.end`: when `p` has ended, an end after `p.end`, or an open one, becomes `p.end`, so a child ends with its parent. An
  end that this cut puts below the end column's declared minimum is `infeasible`: the row keeps the end its own cell drew,
  inside the range, and is counted for the period entry whose parent ended first. Past the window an end follows the lifetime's `endPastWindow` only while `p` is still active there. The order is
  fixed: the end is drawn without the window rule (the duration's end, NULL for its `open` slot, or the end column's own
  value); on a row whose parent has ended it is cut at the parent's end, in the child's units; and only where no parent
  has ended does an end after the window become NULL under `open`. The cut draws nothing and moves no position, the
  start is a date entry on the lifetime's start column (it counts with the date entries for that column), and the
  lifetime must declare an end (`FLX-3105`).
- Order between related rows is the same entry. A payment after its invoice is a `within` entry on the payments' date
  through the invoice relationship, where the invoices declare a lifetime that starts at their date and has no end. A
  ticket that closes after it opens is the ticket's own lifetime.

Chains hold level by level. A parent's start and end are its final values (ADR 0022 section 3), so a parent that is
itself within its own parent is drawn there first. An invoice within its account's lifetime, whose account's period is within its
customer's (`lifetime: true`), falls inside the customer's lifetime on every row that violates neither level. A tie on
the account's start alone keeps the invoice after the customer's start and not before its end. An entry may also name the
chain itself (`account.customer`) to state the outer level directly. The rows of the related exposes are read as ADR 0022
reads them: the plan of a reading expose carries the plan of every expose its `within` entries reach, and a cycle among
them is `FLX-6210`. A date set by `within` or by a duration is a column a rule determines: it keeps its declared NULLs
first (EXPR-SPEC-2 §6), it is ordered after the columns it reads, and a contract `derive` that reads it, such as a status
from the end, reads its final value. Kernel 0.2.0 only, and kernel 0.1.0 refuses either key (`FLX-3105`, naming the
kernel).

**Children that follow their parent's exposure.** A `within` entry over one relationship may carry `exposure: true`
(ADR 0025 §4); `false` and a chain of relationships are `FLX-3105`, as is a second entry with it for one relationship of an
emit, a parent that declares no lifetime and kernel 0.1.0. The entry gives the relationship's counted layout (§8.1 of
KERNEL-SPEC-2) an expected count for each parent through the expected-count hook of ADR 0016 §3, which `expected`
reads off the parent's final row and which draws nothing. A parent's exposure `E` is the integral, over the parent's
lifetime cut to the window (`spec.timeBounds`, both ends included), of the intensity `lambda` of the entry's date column,
and `lambda` is `1` where the column has no `rates` entry. A lifetime whose end is NULL, or before its start, runs to the
window's end; a NULL start, or a lifetime wholly outside the window, gives `E = 0`. `lambda` is constant over each day of a
DATE column, so `E` is the sum of `lambda(day)` over the lifetime's days; for a TIMESTAMP column it is constant over each hour,
and `E` is the sum of `lambda(hour) * overlap_ms / 3 600 000` over the hours the lifetime covers, divided by 24, so that
`E` is in days and a lifetime that starts or ends inside an hour weighs that hour by the milliseconds it holds. Both are
exact rationals. Without a `children` shape the child rows are allotted to the parents in proportion to `E` by the largest
remainder, so a parent with `E = 0` has no child; with one, the parents are ranked by `E` and take the shape's slots in
that order, ties by the layout's permutation. Each child is then dated inside its parent's interval by its own entry, as
above. The entry is bound into `H` as written, and the hook's member (the relationship, the date column, the parent's
lifetime columns and the column's `rates`) is part of the child plan's document, so a changed declaration moves the child's
`generatorDigest`; an entry without `exposure` keeps every byte it had. The hook object, with its declarations, travels
inside the plan to each worker process, so `--jobs 1` and `--jobs 4` lay the children out alike.

**The stream and the violation shares.** Each column that a lifetime's `duration` or a `within` entry sets draws on the
rule stream of EXPR-SPEC-2 §7, with `R` the canonical JSON of `["dates", <column>]` (EXPR-SPEC-2 §7 gives its positions).
An entry's `target` is the percent of covered rows that violate its fact, as a decimal string, as EXPR-SPEC-2 §7 writes a
rule's target. On a covered row whose own value is not NULL, position 0 decides whether it holds, with `s` the target as a
share once the NULLs are taken off (EXPR-SPEC-2 §6). A violating row takes the first of its 16 candidates, drawn from the
column's own statistics, that lies outside the interval, and otherwise the first end of its own declared range that does;
one that finds none keeps a value that holds and is traced as not violated. For a period the violation is drawn on the
start: its end is then drawn from that start by its duration, and the cut at the parent's end does not apply to a
violating row. For a lifetime with a `duration` the violation is an end before the start, drawn as the end column's own
statistics draw; a lifetime with a target and no duration draws its end from its own statistics and only `verify` checks
it. A `within` date is outside the parent's lifetime, and inside every other covering entry's. A target of 0 or none
draws nothing at position 0 and leaves the rows byte for byte. Every violating row is traced as a violation injected on
purpose. With no target the entry generates no violation, and every row it covers holds: an enforced fact fails only on
a defect, never on a row the generator made.

**Seasonal rates.** The namespace key `rates` lists `{emit, column, month, dayOfWeek, hourOfWeek}` entries (ADR 0025
section 2), at most one per column of an emit and at most 4 096 in all. `column` is a DATE or TIMESTAMP column that a
`within` entry sets, that is a period's start, or that is the end of a lifetime with a `duration`; any other column is
`FLX-3105`. Each factor is an optional list of weights written as decimal strings of at least zero, not all zero, with
the digits a weight has elsewhere: `month` has 12 (January first), `dayOfWeek` 7 (Monday first) and `hourOfWeek` 168
(Monday 00:00 to 00:59 UTC first, for a TIMESTAMP column only, `FLX-3105` on a DATE), and at least one is given. The
intensity of a value is `lambda(t) = month[m] * dayOfWeek[d] * hourOfWeek[h]` in UTC, a missing factor counting as 1 and a
DATE counting as its day, and `lambda_max` is the product of each given factor's largest weight, both exact rationals.
The step's own draw becomes proposal 0 of 128, each proposal drawn at position `1 + 2k` of the column's dates stream with
the sampler the direct draw uses and tested at position `2 + 2k` by one `uniform53`, `a`: it is accepted when
`a * lambda_max < lambda(value) * 2^53`, compared as integers. The value is the first proposal accepted. A proposal that is
NULL (an `open` end, or an end past the window under `endPastWindow: open`) is accepted at once. When none is accepted the
value is proposal 0 and the row is traced as a rates fallback. A period's end is thinned before it is cut at its parent's
end, and a violating row's value is a candidate and is never thinned. A column with no entry draws position 1 alone, so
its rows are unchanged. Each entry is bound into `H` of the declaring emit and left out of `G`, and kernel `0.1.0`
refuses the key (`FLX-3105`, naming the kernel). The receipt's `dates` member is present for an emit with a `rates`
entry as well as for one with a target, and each fact then counts `ratesFallbacks` as well.

The 16 candidates of a column take `m` positions each, from 257: one call for a uniform draw over a range and for a rate,
two for a histogram (a `categorical` then a `bounded`), so candidate `j` sits at `257 + j * m`. A TIMESTAMP value a step
sets outside the milliseconds a TIMESTAMP holds (years 1 to 9999) is `FLX-6112`, as a DATE out of range is.

**What the receipt counts.** The receipt's emit entry carries a `dates` member only for an emit whose facts carry a
target above zero: per fact id (`lifetime:<start>.<end>`, `within:<relationship>.<column>`), the counts `held` (rows that
hold the fact), `drawn` (rows violated by a candidate or an end of the declared range), `infeasible` (rows whose levels'
lifetimes did not meet, or where `J ∩ R` was empty, counted for the outermost covering entry, and rows of a period whose
end the parent's end cut below the end column's range), `notViolated` (rows that drew a violation and found no
value outside the interval, so they hold) and `offsetFallbacks` (and `ratesFallbacks` for an emit with `rates`). A row whose own value is NULL, or whose lifetime has no
end, is not covered and is not counted. The evidence pack lists, per fact, the violations injected on purpose (`drawn` and
`infeasible`). A run without a target keeps its receipt.

**What `verify` scores of them.** `fluxsim verify` scores each fact as a line of its `rules`: `lifetime:<start>.<end>` for
each lifetime, `within:<relationship>.<column>` for each `within` entry (`within:<relationship>.lifetime` for a period),
and, for each chain of entries, the outer level as `within:<relationship>.<relationship>.<column>`, so every level of a
chain is scored. A fact with no target is enforced: a violating row fails the run, as a `derive` without a band does. A
fact with a target is checked against it. The implied outer level of a chain is checked against the sum of its levels'
targets, zero when every level is enforced, unless an entry states that outer level itself, whose own target then holds.
An outer level is implied through period ties only; a date tie on a middle expose's start makes no implied line.
A `within` entry with `exposure: true` also makes the line `exposure:<relationship>` where the profile publishes an `exposure`
for the parent's lifetime columns (ADR 0025 §8): the run's children per active day, the child rows whose parent's start is
known over the whole days those parents are active inside the window, against the profile's `children / activeDays`, within
the relative tolerance of a `facts` entry that names the line, and 10 % otherwise. A withheld count, a hidden table or a
hidden column leaves the line listed as not scored, with the reason. Each fact is named `<exposeId>.<ruleId>` with these ids, so a `facts` entry may give it a tolerance, the profile's
`invariants` section may hold its measured share, and the scorecard's `invariants` dimension scores it as ADR 0018 scores
any fact. The run's streams hold no hidden column and no hidden table, so a fact that reads either is listed by
`fluxsim verify` and by the evidence pack as not scored, with that reason, and never counts as passing. Verify reads the
rows a run wrote and does not generate rows, which keeps it the independent check it is; regenerating hidden rows inside it
would check the generator against itself.

```yaml
extensions:
  com.agentics.flux/1:
    emits:
      payment_recovery_moment:              # keyed by the emit's exposeId
        rows: 1000                          # §1's declared row count
        mapping:                            # optional per-column source overrides (§1)
          aborted_at: event.time
        invariants:                         # optional FLUX settings for the contract's rules, keyed by the
          date_of_key:                      # invariant's id (EXPR-SPEC-2.md §1)
            generates: d_date               # the column a check of difference form generates
        rules:                              # optional FLUX-side rules (EXPR-SPEC-2.md §1), a list
          - id: day_of_month
            language: flux-expr/3
            derive: d_dom
            expr: EXTRACT(DAY FROM d_date)
        fidelity:
          encoding: { fractionalBits: 16, rounding: floor }
          records: { min: 1000, max: 100000 }
          columns:
            amount:      { range: { min: "0", max: "10000" }, null: exclude }
            market:      { support: { values: ["IE","GB","FR","DE"] }, maxCardinality: 8, null: fail }
            aborted_at:  { range: { min: "2026-07-01T00:00:00Z", max: "2026-09-30T23:59:59Z" }, null: fail }
          properties:
            - { property: mean, column: amount, tolerance: { type: relative, value: "0.02" } }
            - { property: marginalHistogram, column: amount, bins: ["0","50","100","250","1000","10000"], tolerance: { type: distance, measure: totalVariation, max: "0.05" } }
            - { property: support, column: market, tolerance: { type: count, max: 0 } }
            - { property: mean, column: amount, groupBy: market, tolerance: { type: relative, value: "0.03" } }
          budget: { maxChecksPerState: 20, maxStateAge: P30D }
          failureAction: withholdBundle
spec:
  emits:
    - productRef: telco.gold.payment_recovery_moment
      exposeId: payment_recovery_moment
      fluidVersion: "^0.7.3"
```
Rules enforced by `fluxsim validate` (offline): every `column` and `groupBy` is a member of the referenced expose
schema; every key of `extensions[...].emits` is an `exposeId` the Simulation actually emits; bins are strictly
increasing and partition the declared range; a `levelHistogram` names a `support` column and carries no `bins`;
a `quantile` may carry `bins`, and no other kind but a `marginalHistogram` does;
a `correlation` property declares two columns and a `minVariance`;
every property's parameters are present (a contract missing any of them is refused before any statistic is
formed). Numbers are strings (exact decimals), never floats. Written out without an exponent, a number has at
most 1000 digits, not counting a leading `0.`: `"1E+99999999"` names a number of a hundred million digits and
is refused (`FLX-5030`) by its length, before its value is formed.

**A block may list no property.** `properties` is a section the block must carry, and it may be an empty list: the
table then declares what it draws from and what its rows may hold (`columns`), and nothing the check could compare, as
a table whose columns the profile gives only a minimum and a maximum has nothing a statistic of the profile answers.
A missing `properties`, and one that is not a list, are refused as before (`FLX-5034`, `FLX-5030`), and a block with
no empty list reads and digests as it always did. `verify` reports such a table as not scored, in its fidelity line,
the scorecard's table line and `EVIDENCE.md`; it never reads as verified, and it meets no threshold or `minCredibility`,
since no score forms from it. When the pinned profile publishes a statistic a property of the table could be scored
against, `verify` adds a note naming it and does not fail, so an empty list cannot hide a check the profile supports.
`fluxsim init` writes the empty list for a table the profile gives no statistic to score, such as one whose columns
have only order statistics, and says the table has nothing to verify; it writes no property the profile has no
statistic for, since `verify` would refuse it (`FLX-5037`).

**A DATE column's bounds are days.** A `range` bound or a `bins` edge of a column whose declared type is DATE is
spelled `YYYY-MM-DD`, or as a whole number of days since 1970-01-01 (the two spell the same bound: `2026-01-01`
is `20454`). Every bound of the column is in days: its range, its bins, a mean's absolute tolerance and the
statistics formed over it, and a value is read as the day it names. Any other string is `FLX-5030`; a value
that is no calendar date, or lies outside the range, is `FLX-5005`. A DATE column whose bounds are RFC 3339
instants reads them as it did before days were a spelling: as a timestamp column in milliseconds, so a block a
profiler declared with instants reads, and profiles, as before. A TIMESTAMP column's bounds are still RFC 3339
instants or decimals (milliseconds). A profile holds a DATE
column as a timestamp in milliseconds; PROFILE-SPEC §7 says how the check reads one.

**A `quantile` may carry `bins`.** They follow a `marginalHistogram`'s rules: strictly increasing, partitioning
the declared range, on the grid. A quantile with bins is formed and compared by `quantile/2` (ADR 0012), which
takes the order statistic as `quantile/1` does and states, in place of its value, the bin that holds it and the
records below and through that bin. Its tolerance is absolute, as every quantile's is, and the check passes when
the synthetic order statistic lies within `τ′` grid steps of the reference bin (§5). A quantile without bins is
`quantile/1`, unchanged.

**`columns.<name>` is one schema, read by two consumers.** `range`, `support`, `maxCardinality` and `null` are
read by the check (§5); `allocation`, `bins`, `binWeights`, `binWeightsGiven`, `family`, `given`, `identifierLength`,
`levelWeightsGiven`, `levels`, `mean`, `nullFraction`, `rate`, `stddev` and `unique` are read by the §1 generator. The set is closed and shared
(`concordance.predicate.COLUMN_KEYS`): a key outside it is refused by both, and neither refuses a key the other
owns. A column entry that declares neither `range` nor `support` declares nothing the check can compare; it is
legal (the generator still reads it), and a *property* naming such a column is `FLX-5034`.

**`support` has exactly two spellings.** `{ values: [...] }` writes the levels out. `declared` takes them from
the FLUID column type's own enum list: `type: "enum('IE','GB')"`, the only structured value set FLUID 0.7.5
carries (`validationRules[].constraint` is free text for `support`, with no grammar; `fluxsim init` reads the range grammar, `between A and B` or `>=`, `>`, `<=`, `<` joined by `and`, from a `range` rule). A column that spells `declared` over a
type naming no levels is refused (`FLX-5034`) rather than resolved to an empty support, because an empty support
makes every value a non-member and fails the check for the wrong reason. A bare list is refused: two spellings of
one declaration would be two digests of one contract.

**A boolean is a category.** A `BOOL` column is categorical (the first table above), and a boolean cell reads as the
category `"false"` or `"true"`: a `support` or `levelHistogram` over it declares those two levels, and the shape flags
(SCORECARD-SPEC.md §6) read it the same way. A boolean is no number, so a range property over a `BOOL` column still
refuses it (`FLX-5005`).

**A distance tolerance may follow the row count.** A `distance` tolerance takes one optional key beyond `type`,
`measure` and `max`: `sampling`, a boolean (anything else is `FLX-5030`, and so is the key on a tolerance that
is not a distance). With `sampling: true` the verifier widens the margin, group by group, by the widening of
SCORECARD-SPEC.md §9a, which grows as the run's rows in the group shrink; for a column declared
`allocation: exact` only its rounding term remains. A tolerance without the key reads and compares as it did,
and the key sits in the tolerance, so it moves `H` and leaves `G` as every tolerance does (§3).

**A whole number is a category.** A column the profile or the declaration reads as categorical may be an `INT` column
drawn over a few integer levels (the table above allows `categorical` for `INT`). A whole number cell of such a column
reads as the category of its decimal text, as `7` or `-3` (no sign for a positive number, no leading zero), in every
form the reader takes a category in: a `support`, a `levelHistogram`, a `groupBy` key and the shape flags. A decimal
(`Decimal`), a float and every other class that is not a string, a boolean or a whole number still refuses
(`FLX-5005`, naming the row and the class). Version 0.9.0 refused a whole number there, so a run it verified whose
property read an `INT` column failed with `FLX-5005`; reading the number changes no row and no `runDigest`, and
`verify`'s reproducibility compares the `runDigest` with the golden baseline and no verdict, so a run made before
verifies as reproducible as it did.

**`levelHistogram` counts a categorical column.** It names a `support` column, whose declared levels, NFC-normalised
and sorted, are its cells, as a `marginalHistogram`'s declared bins are its cells. It carries no `bins`, and its
tolerance is a total-variation distance. With `groupBy` (a second `support` column) it is a two-way count table of
the two columns: one count per (group, level) cell, compared group by group as a grouped `marginalHistogram` is.
A value of either column outside its declared support has no cell, so it is refused (`FLX-5005`) rather than
counted, as a value outside a declared range is. Every group needs `records.min` records, as every group does. A
declared level the state never holds is a cell of count zero, so the table's shape comes from the declaration and
never from the data.

**`given` draws a column conditionally on another.** `given` names one other column of the same expose, and the
column declaring it carries one weight table per level of that column, in place of its unconditional weights:

- a histogram column (declared `bins`) declares `binWeightsGiven` in place of `binWeights`: for each level of the
  given column, one `binWeights` list over the same edges, P(bin | level). A grouped `marginalHistogram`
  property (`groupBy` the given column, over the same bins) states exactly these lists: its (group, bin) counts;
- a categorical column declares `levelWeightsGiven` in place of `levels`: for each level of the given column, one
  mapping of level to weight, P(level | level), from a two-way count table. Every mapping names the same levels.

The weights are counts or shares, normalised per level of the given column as `binWeights` is, and every list or
mapping carries some weight. The given column is a categorical with no `nullFraction`, so each row has exactly
one level to look up, and the table names exactly the levels it can draw. The links form a forest: a column has
at most one `given`, a cycle is refused, and a histogram column is always a leaf, because a bin is not a level.
A column is drawn after the column it is given, whatever the declared column order; every cell keeps its own
§1 path, so column-first generation and slices are unchanged, and a root of the forest (a column drawn given
nothing) draws exactly the values it drew without the links. A declaration the
generator cannot draw is refused at plan time with `FLX-3402`.

**`given` may name several columns.** A list of two or more names, `given: [region, plan]`, draws the column given
the combination of their levels in each row. The weight table is nested, one mapping per given column in the order
`given` lists them, ending in the column's own weights: `levelWeightsGiven: { east: { basic: { online: 9, store: 1 } } }`
(and `binWeightsGiven` ends in a `binWeights` list). It names exactly one entry for every combination of the levels
the given columns draw, so a combination the data never holds still names its weights. A list names at most 8 columns, and a list of one name is refused,
so one parent has one spelling, the plain name, and every declaration that reads today reads and hashes as it did.
Every given column is a categorical with no `nullFraction`, as for one. The links now form a directed graph, not a
forest: a column has any number of parents, a cycle through any of them is refused, and a column is drawn after
all of its parents. The cell is drawn from its own §1 path exactly as a column drawn given one column is, and the
combination only picks the weights, so no seed path is added and the draws of every other column and plan are the
bytes they were. The column path of kernel 0.2.0 searches each row's uniform in the cumulative table of its
combination, and the per-row path is its oracle. A plan that declares no `given` documents none of
the three keys, so its `generatorDigest` and its rows are the ones it had before they existed.

**`given` may reach across a link.** `given: <relationship>.<column>` draws the column given a column of the row a
to-one relationship of its expose names, with `levelWeightsGiven` or `binWeightsGiven` keyed by that column's levels,
exactly as for a column of the same row. The level a row draws given is the level of the parent's final value at the
parent row the relationship names, the value the parent expose's own pass writes there (EXPR-SPEC-2.md §6, rules
over a relationship). It is `FLX-3402` at plan time, before any row exists, unless the relationship is one of the
expose that reaches a row on every row (its first column declares no `nullFraction`, and no `links` condition is
declared on it), its columns are set by a foreign key or key part of the expose and by no rule, and the parent
column is a categorical drawn from its own declared levels with no `nullFraction`, the `to` column of a contract
dependency, or a column of a reference expose (§1, whose levels are its distinct published values), and no `derive`
or check rule sets it; the weight tables name exactly the parent's levels. One link only: a path of two links is
refused, and so is a list of given columns that holds a path. Kernel 0.2.0 only: kernel 0.1.0 refuses it
(`FLX-3402`, naming the kernel). The cell is drawn on its own §1 path at the index it always takes, so no seed-tree
path is added (KERNEL-SPEC-2.md §12); a plan that declares none documents none of it, and the plan lists the plan it
reads. The column path of kernel 0.2.0 leaves such a plan to the per-row path, its oracle.

**`allocation` makes a column's classes exact.** `allocation: exact` gives each class of the column exactly its share of
the expose's rows, in place of a share each row draws for itself: the column's levels (with `levelWeights`, or equal
weights over its declared support), or its bins with `binWeights`, then NULL last when it declares a `nullFraction`,
take `LRM(N, weights)` rows, `N` the expose's rows after `--scale`, and each row takes the class of one slot of a
permutation keyed by the path `("sim", sim_id, "gen", exposeId, "allocate", column)`. A bin's value inside the bin is
drawn as always. A column drawn given another column that is itself exact holds exactly its share of rows within each
level of the given column, so each pair of a parent level and a child level holds its own count. `exact` is the one
value, and any other is refused (`FLX-3105`). The key is read for a column of the families above only: a mean and a
standard deviation, a rate, an identifier, a column given a column of a linked row and a
column given a column that is not exact are `FLX-3402`. Kernel 0.2.0 only: kernel 0.1.0 refuses it (`FLX-3105`,
naming the kernel). KERNEL-SPEC-2.md section 8.3 is the rule. The cell makes every draw it made before, at the index
it always takes, so a plan that declares no `allocation` documents none of it, and its rows and `generatorDigest` are
the ones it had.

**A name with a dot is a column when the expose holds it.** A column of an expose may be named with a dot, as
`tier.code`. Wherever a declaration writes a dotted name (`given`, a `fidelity.columns` key for a column of a linked
row, a property's `groupBy`, and what `fluxsim init` writes for them), the exact name of a column of the expose wins:
the name is that column, whatever the contract's relationships are called. A dotted name is a link only when no
column of the expose has that exact name and its first part names a relationship the contract declares for the
expose (`FLX-3402`, or `FLX-5031` for a group key, when it names none). A given list names columns of the expose, so
a dotted name in one is accepted only when it is such a column. `fluxsim init` writes no given and no group key
for a link whose written name is the name of a column of the expose, and lists the statistic as unused with that
reason, since the reader would take the name as the column. A column that holds the name of a link therefore
cannot be reached by the link form of the same name; it is reached by its own name.

`flux_engine.calibrate.structure` suggests the forest offline, from two-way count tables alone: a Chow-Liu tree
(Chow and Liu, 1968), the maximum spanning tree of pairwise mutual information, which is the selection step of
the MST synthesizer (McKenna, Miklau and Sheldon, 2021) without its privacy noise, with every numeric column a
leaf. It reads counts and writes the `columns.<name>` entries; it never reads a record. Counts can still single
out a rare combination: a cell of one or two counts describes a handful of real records, so the owner applies
their own disclosure rule, such as a minimum cell count, before declaring a table.

**What the check covers of a `given` link.** Both kinds of link are checked end to end, by a grouped property
over the drawn column with `groupBy` the given column. A histogram drawn given a categorical is checked by a
grouped `marginalHistogram` over the same bins, and a categorical drawn given a categorical by a grouped
`levelHistogram`. Each compares D′'s (group, cell) counts with S's, group by group. They are the counts the link is
declared from: a grouped `marginalHistogram`'s (group, bin) counts are `binWeightsGiven`'s lists, and a grouped
`levelHistogram`'s (group, level) counts are `levelWeightsGiven`'s mappings. `flux_engine.calibrate.structure`
reads them back as two-way tables (`pair_table_from_groups`, `pair_table_from_level_groups`), so the forest can be
suggested and declared from a committed S. A root's own `levels` are checked by an ungrouped `levelHistogram` over
it. A pair of columns that no declared property counts together is not checked.

**A property may be grouped by a link.** `groupBy: <relationship>.<column>` groups the property's column by `<column>`
of the row a to-one relationship of the expose names, as `given: <relationship>.<column>` draws it. The grouping column
is declared in `columns` under the same dotted name, with its `support` and `maxCardinality` and no other key: its
levels are the cells of the groups, and a `levelHistogram` counts only those. The relationship is one the contract
declares for the expose (`FLX-5031` otherwise). `fluxsim verify` forms each group by joining every row of the emit to
the row of the relationship's target that its `fromColumns` name, by the target's `toColumns`, in the target's emitted
rows, and reading `<column>` there as the group key. A row whose link reaches no row (a null key column, or a key no
target row holds) has no group, as a row with a null grouping column has none, and is never compared. The reference
is the profile's statistic of format 1-3-0 whose `groupByLink` names the same target table, key columns and column
(PROFILE-SPEC.md section 3); a profile that holds only another grouping of the column is `FLX-5037`. A target
emit that cannot be read refuses the emit (`FLX-8001`). The property reads another emit's rows, so a committed slice
and a sample of blocks, which hold one emit's rows, do not score it: the slice leg refuses it as a column the rows do
not carry (`FLX-5031`), and `--sample` refuses the emit naming the same reason. The generator reads nothing from the
dotted `columns` entry.

## 3. Digests

- `H` = `jcs-document` digest of `{"contract": <parsed .fluid.yml>, "emit": <emits[] entry>, "extension": <the emit's entry in `extensions["com.agentics.flux/1"].emits`, when it has one>, "columns": [declared column names in order]}`. The `extension` key is **present only when the emit has such an entry**, so a bundle that declares none keeps the `H`, the seed root and the whole stream it had before the block existed. Computed by `fluxsim plan`, printed, and stored in `plan.json`; the run seed derives from it (KERNEL-SPEC §3).
- `G` = the same with the **excluded set removed** from the emit entry and from the extension block: every `tolerance`, every `minVariance`, `budget.maxStateAge`. `G ⊂ H`. A change that leaves `G` unchanged keeps the committed statistic reusable.
- A **fact with a tolerance** (`extensions["com.agentics.flux/1"].facts`, ADR 0018) is part of the run's fidelity predicate, so it is bound into `H` before any row is drawn. The emit's `extension` member gains a `facts` key, `{"<ruleId>": {"tolerance": "<decimal as written>"}}`, **only when the Simulation declares a fact with a tolerance for that emit**, so every other emit and every bundle that declares none keeps its `H`. `G` leaves the `facts` member out, as it leaves out every tolerance: an emit whose only extension entry is `facts` has the `G` it had without it.
- A **link entry** (`extensions["com.agentics.flux/1"].links`, ADR 0022 section 5) is a list of `{emit, relationship, when, sentinels, weight}` entries, each naming an emitted expose and a relationship it declares, with at least one of the other three. `weight` names a number column of the relationship's target, and the parent row is then picked by the weights of the parent's final rows (ADR 0023 section 4, KERNEL-SPEC section 8.2). It changes the rows the emit generates, so it is bound into `H`: the emit's `extension` member gains a `links` key, a list of `{"relationship", "when", "sentinels", "weight"}` (each key only when declared) in relationship order, **only when the Simulation declares an entry for that emit**, so every other emit and every bundle that declares none keeps its `H`. `G` leaves the `links` member out. The namespace's closed set of top-level keys gains `links`, and kernel `0.1.0` refuses the key.
- A **bridge** (`extensions["com.agentics.flux/1"].bridges`, ADR 0022 section 8) is a list of `{emit, relationships: [a, b]}` entries, each naming an emitted expose and two distinct relationships it declares. It changes the rows the emit generates, so it is bound into `H` the way a link entry is: the emit's `extension` member gains a `bridges` key, a list of `{"relationships": [a, b]}` in name order, **only when the Simulation declares a bridge for that emit**, and `G` leaves the member out. The order of the two names is part of `H`, since `a` is the major part of the pair layout. The namespace's closed set of top-level keys gains `bridges`, and kernel `0.1.0` refuses the key.
- A **counts entry** (`extensions["com.agentics.flux/1"].counts`, ADR 0016 section 1) is a list of `{emit, relationship, minChildren, parentWhen, children, tolerance}` entries, each naming an emitted expose and a relationship it declares, at most one entry per relationship. It puts the relationship on the counted layout (KERNEL-SPEC-2 section 8.1): the child's rows take their parents from a layout that gives each parent a whole number of children. `minChildren`, a whole number of 0 or more (an integer or decimal text, anything else is `FLX-3105`), is the least number of children of every parent that may have any, and `parentWhen`, a FLUID SQL/1 predicate text over the parent's own columns, says which may: only a parent whose final row makes it TRUE has children (NULL and FALSE do not). A `parentWhen` that reads a link, a previous version, children, siblings or a column a summary over the parent's children sets is `FLX-3403`. `children` is the shape of the number of children per parent, exactly one of `{levels, levelWeights}` and `{bins, binWeights}` with every entry a string, and `tolerance` a decimal text between 0 and 1. No other key is read, and an unknown key is `FLX-3105`. It changes the rows the emit generates, so it is bound into `H` the way a link entry is: the emit's `extension` member gains a `counts` key, a list of `{"relationship", "minChildren", "parentWhen", "children", "tolerance"}` (each key only when declared) in relationship order, **only when the Simulation declares an entry for that emit**, and `G` leaves the member out. The namespace's closed set of top-level keys gains `counts`, and kernel `0.1.0` refuses the key.
- **Copies** (`extensions["com.agentics.flux/1"].copies`, ADR 0022 section 4) is the top-level key `copies: true`, whose only value is `true` (anything else is `FLX-3105`). Under kernel `0.2.0` it makes the emits that list a `sharedColumns` or a `sharedAttribute` copy them (EXPR-SPEC-2 §9); without it the two declarations are read as declared and not generated, so a plan written before copies existed keeps its bytes and digests (KERNEL-SPEC-2 section 12). It changes the rows the emit generates, so it is bound into `H` the way a link entry is: the emit's `extension` member gains `"copies": true`, **only when the Simulation declares it and the emit's expose lists something to copy**, and `G` leaves the member out. The namespace's closed set of top-level keys gains `copies`, and kernel `0.1.0` refuses the key (`FLX-3105`, naming the kernel). `fluxsim init` writes it for a contract that declares a `sharedColumns` or a `sharedAttribute`, and for no other.
- A **generator plugin** (`extensions["com.agentics.flux/1"].generators`, ADR 0032 section 2) is a mapping from an emitted `exposeId` to `{plugin, distribution, version, grade, reproducibility}` and, as the grade asks, `privacy`, `model` and `options`. `plugin` is an entry-point name (`^[A-Za-z][A-Za-z0-9_.-]*$`), `distribution` and `version` pin the distribution that provides it, `grade` is `P4` or `P5` (anything else is `FLX-3105`), and `reproducibility` is `bytes`, `environment` or `none`. A `P4` declares `privacy` (`unit` and `accounting`, non-empty strings of at most 1000 characters, and `epsilon` and `delta` or `rho`, exact decimal strings, with epsilon and rho positive and delta at least 0 and below 1) and `model.digest` (`sha256:` and 64 lowercase hex digits); a `P5` declares no `privacy` and may declare the digest. `options` is plain data, and a mapping key anywhere inside it that names a credential is refused, naming the key and never a value. The declaration changes the rows its expose generates, so it is bound into `H`: the emit's `extension` member gains a `generators` key, the declaration as written (each part only when declared), **only for the exposes the Simulation declares a plugin for**, and `G` keeps it. The run's grade is the weakest of the profile's and every plugin's, which the governance gate checks (`FLX-4005`). The namespace's closed set of top-level keys gains `generators`, and kernel `0.1.0` refuses the key (`FLX-3105`, naming the kernel). Nothing in this record imports or calls a plugin.
- **Lifetimes, `within` entries and hidden declarations** (`extensions["com.agentics.flux/1"].lifetimes`, `.within` and `.rates`, ADR 0024 sections 4 and 5 and ADR 0025 section 2; an emit's `hidden` and `hiddenColumns`, ADR 0024 sections 2 and 3) change the rows the emit generates, so each is bound into `H` the way a link entry is: the emit's `extension` member gains a `lifetimes` key (the emit's entry, each key only when declared), a `within` key and a `rates` key (each a list of the emit's entries in the order declared, each key only when declared), `"hidden": true` and a `hiddenColumns` key (the list as declared), **only when the emit declares it**, so every other emit and every bundle that declares none keeps its `H`. `G` leaves all five members out, as it leaves out `links`. The namespace's closed set of top-level keys gains `lifetimes`, `within` and `rates`, and kernel `0.1.0` refuses each of the five (`FLX-3105`, naming the kernel).
- A **history entry** (`extensions["com.agentics.flux/1"].history`, ADR 0017 section 2) is a list of `{emit, entities, parent, versions, start, length, until, chain, openEnd, activation, tracked, reasons, end}` entries, one per versioned emit, each naming an emitted expose whose contract declares a validity window. `emit`, `entities` (a whole number above zero), `length` and `until` are required, and every other key is optional: `parent`, with the keys `minChildren`, `parentWhen` and `children` of a counts entry; `versions`, `start` and `length`, histograms, `versions` in the spelling of `children`, `start` with its edges inside `[timeBounds.start, until]` and `length` written as EXPR-SPEC-2 section 6 writes a `distribution` (`bins`, `binWeights`, `closed`), drawn at the chain's unit, with no `open` weight, `mean` or family spelling (`given`, `states`, `otherwise`), each `FLX-3105`; without `versions` the versions are spread evenly over the entities; `chain`, `nextStart` (the default) or `dayBefore`, under which a version's start is not after its end; `openEnd`; `activation`, a column; `tracked`, a list of `{column, initial, transitions}`; `reasons`, `levels` with `levelWeights`, required when more than one column is tracked; and `end`, `{mode, share, column, value}`, with `mode` `closeLast` or `finalVersion`. No other key is read, and an unknown key is `FLX-3105`. These are refused at plan time with `FLX-3403`: an emit without a validity window, or with a start and an end of different types; a second entry for one emit; a `parent` that is not a to-one relationship of the emit, that a `counts` entry also names, or that a `links` entry gives a `when` or a `weight`; a `versions` class below 1; an `until` after `spec.timeBounds.end` or not after `spec.timeBounds.start`; a `start` bin outside `[timeBounds.start, until]`; a `length` bin edge below one day; `dayBefore` on TIMESTAMP columns; an empty first-start interval (KERNEL-SPEC-2 section 8.4); a primary key other than one column, or than the natural entity's columns and the start; an `openEnd` on a nullable end column that is not the column's type, or no `openEnd` on an end column the contract requires; an `activation`, tracked or end column that is a key, a validity column or a column a rule sets; a DATE or TIMESTAMP column of the emit that declares no range; and the refusals of ADR 0017 sections 5 and 6 (an `end` of `finalVersion` without a `column`, a `share` that would end more entities than are eligible, an `end.value` that a tracked column or a carried column can also take, a tracked column that is not categorical or has a `nullFraction`, a level a chain can reach with no weight, a carried column drawn `given` a tracked column), and every refusal reads declared values, the scale and the layout's counts, never a drawn row. A `lifetimes` or `within` entry on the start, the end or the activation column of a history's emit is `FLX-6203`. It changes the rows the emit generates, so it is bound into `H` the way a link entry is: the emit's `extension` member gains a `history` key holding the entry, **only when the Simulation declares an entry for that emit**, and `G` leaves the member out, and no plan document enters it. Kernel `0.1.0` refuses the key (`FLX-3105`, naming the kernel). The namespace's closed set of top-level keys gains `history`. Its layout, chains and draws are KERNEL-SPEC-2 section 8.4.
- An **as-of entry** (`extensions["com.agentics.flux/1"].asOf`, ADR 0017 section 8) is a list of `{emit, relationship, date}` entries. `relationship` is a to-one relationship of `emit` whose target is a history's emit and whose `toColumns` are that history's natural entity, and `date` is a DATE or TIMESTAMP column of `emit`; anything else is `FLX-3403`. A path through the relationship reads the version of the linked entity valid on the row's `date`, and reads NULL when no version is valid. Between a DATE and a TIMESTAMP a DATE is the instant of its midnight UTC, except that under `dayBefore` a DATE end covers its whole day, so a DATE `date` read against a TIMESTAMP history reads the version valid at that date's midnight UTC. A column the reading emit's contract does not leave nullable, set by a rule that reads through the link, is `FLX-3403` at plan time, unless the history has no `end` and the declared range of `date` starts at or after `until`, so that every row finds a version. A `distinct` or `one_to_one` relationship into a history's emit, and a `counts` entry on one, are `FLX-3403`, and a `date` that a rule sets by reading through the same link is a cycle, `FLX-6208`. Without an entry, a path into a history's emit that reads a tracked column, a validity column or a column a rule sets is `FLX-6106`, and the message names `asOf`. It changes the rows of the reading emit, so it is bound into `H` the way a link entry is: the emit's `extension` member gains an `asOf` key holding the entries, **only when the Simulation declares an entry for that emit**, and `G` leaves the member out; the reader's plan document carries the history's plan, which enters `planDigest` and never an entry for `H`. Kernel `0.1.0` refuses the key (`FLX-3105`, naming the kernel), and the namespace's closed set of top-level keys gains `asOf`.
- A **projected emit** (ADR 0027) is an emit that a `snapshots`, `events` or `periods` entry names. Its rows follow from the history of the entry's `of`, which names an emit that a `history` entry declares. It declares no `rows`, and a declared one is `FLX-3403`, naming the entry. At most one entry names an emit, and a projected emit with a `history` of its own, a `reference` block, or a `counts` entry over one of its relationships is `FLX-3403`, as is a relationship whose parent is a periodic emit. A history with no entity projects empty emits. Each of the three keys below changes the rows of the emit it names, so it is bound into `H` the way a link entry is: the emit's `extension` member gains the key, holding the entry, and a member `projects` that holds the history's emit and that emit's own entry for `H`, which holds its `history` entry, so a changed history moves its projections, **only when the Simulation declares an entry for that emit**. No plan document enters an entry for `H`: a plan document holds the window, and the seed tree's root folds in every `H`. `G` leaves both members out. The namespace's closed set of top-level keys gains `snapshots`, `events` and `periods`, and kernel `0.1.0` refuses each with `FLX-3105`, naming the kernel. The projected emit's number of rows is worked out at plan time and recorded in the plan document, where it enters `planDigest` only (KERNEL-SPEC-2 section 8.5).
- A **snapshots entry** (`extensions["com.agentics.flux/1"].snapshots`, ADR 0027 section 3) is a list of `{emit, of, ended}` entries. `ended` is `keep`, the default, or `drop`. Each column of the snapshot's contract is a column of the history's emit of the same name and type, else `FLX-3403`. The key of the snapshot is the natural entity.
- An **events entry** (`extensions["com.agentics.flux/1"].events`, ADR 0027 section 4) is a list of `{emit, of, date, at, lag, on, op, type, before, after, when}` entries. `date` is required and names a column that holds when the event was recorded; `at`, optional, names a column of the same kind that holds the change's instant, DATE or TIMESTAMP. `lag`, optional, is a histogram whose edges are whole days, written as EXPR-SPEC-2 section 6 writes a `distribution` (`bins`, `binWeights`, `closed`). `on` is a list of `c`, `u` and `d`, all three by default. `op`, optional, names a text column. `type`, optional, is `{column, values}`, where `values` maps `c`, `d`, `end` and each tracked column to the text written in `column`; every kept op, every reason, and `end` when the history has ending versions, needs a text, else `FLX-3403`. `before` and `after` map an event column to a column of the history's emit; a `before` column the event contract requires is `FLX-3403` when `on` keeps `c`, and an `after` column it requires is `FLX-3403` when `on` keeps `d`. On the `d` of a `closeLast` entity `before` holds the last version's own value. `when`, optional, is a FLUID SQL/1 predicate over the version the event is about, and a predicate that reads any other column is `FLX-3403`. No other key is read, and an unknown key is `FLX-3105`.
- A **periods entry** (`extensions["com.agentics.flux/1"].periods`, ADR 0027 section 5) is a list of `{emit, of, column, grain, match, kinds}` entries. `column` names a DATE column of the emit that holds the period's first day. `grain` is `day`, `week`, `month`, `quarter` or `year`, and defaults to the period column's FLUID `timeGranularity`, `FLX-3403` when neither gives one. `match` is `overlap`, the default, `end` or `start`. `kinds` is a list of `{count, when, overrides}`, `count` a whole number or `levels` with `levelWeights`, `when` a FLUID SQL/1 predicate that reads no column a cell draws (`FLX-3403` otherwise), and `overrides` a map from a column to a text written as the column's type; without `kinds` there is one kind, one row per pair. No other key is read, and an unknown key is `FLX-3105`.
- A **profile overlay** (`extensions["com.agentics.flux/1"].overlay`, ADR 0030 section 7) is a mapping of exactly `name` and `patch`: a named RFC 7396 merge patch over the profile the Simulation pins. `name` matches `[a-z][a-z0-9-]*` and has at most 63 characters, and `patch` is a mapping that may touch only a table's `rowCount`, a column's `metrics` and `statistics`, a table's `columnPairs` and `periods`, and the top-level `relationships` and `invariants`. A patch replaces a list whole, a `null` removes a member, and a statistic the patch restates carries no `noise`. Anything else (`_schemaURL`, `encoding`, `disclosureRules`, `privacy`, `references`, `queries`, a column's `domain`, a table or a column the pinned profile does not hold) is `FLX-3105`, as is an overlay in a Simulation that pins no profile. The overlaid profile is `MergePatch(profile, patch)`, read by flux-stats' profile reader at the pinned profile's format, so every bound and exactness rule of a profile holds for it; `plan` refuses a patch that makes a document the reader refuses. The privacy grade keeps reading the pinned, unpatched profile (ADR 0020). `plan.json` records the overlay as `overlay`, `{name, patch}`, so `verify` and `ship` read it through the plan's digest. The key is part of the run's fidelity predicate, so it is bound into `H`: every emit's `extension` member gains `"overlay"`, the `jcs-document` digest of `{name, patch}`, **only when the Simulation declares an overlay**, and `G` leaves the member out, since the overlay changes no row and no committed state. The namespace's closed set of top-level keys gains `overlay`, and kernel `0.1.0` refuses the key (`FLX-3105`, naming the kernel).
- `ε` per property, computed by the compiler from `encoding`, `columns[].range`, `records.min` (and `minVariance` for correlations) by interval propagation; `τ′ = τ − ε`; `ε ≥ τ` → `FLX-5002` at plan time; `τ′` rounded onto the grid in the tightening direction.
- Both `H` and `G` and every `ε` are written to `plan.json`. The emit's entry carries the compiled block under `fidelity`, the key the block is declared under: `exposeId`, `H`, `G`, `encoding`, `fieldCapacityBits`, and per property its `epsilon`, `margin` (`τ′`), `maxNonMembers` and `magnitudeBits`. An emit that declares no block carries no such key.

## 4. Boundary side (`concordance/boundary/`, invoked by `fluxsim apply --boundary` from `Playback.sourceStream`)

- **Committed state**: the owner fixes the record set (`file://` slice in v0.1; partition/sequence boundary recorded); `stateOrdinal` increments per state.
- **C1**: per-column Merkle root (sha256 in v0.1; the leaf function is a port so it can become circuit-friendly later) over fixed-point-encoded values padded to `records.max` with the reserved padding encoding; nulls carry the null encoding; a validity flag per position.
- **S** (derived statistic): per property the aggregate quantities only: bucket counts on the declared edges, level counts on the declared support, `Sx`, `Sy`, `Sxy`, `Sxx`, `Syy`, the sorted committed support, the quantile witness (for a quantile with bins, the bin that holds it and the records below and through that bin, never the witness), `n`, `k` per property and per group.
- **C2**: sha256 over canonical `S` plus a per-state blinding value that never leaves the boundary.
- What leaves the boundary in v0.1: `S`, `C1`, `C2`, `stateOrdinal`, record count (or declared range), commitment time. **Never a record.** The check counter (`budget.maxChecksPerState`) lives here and refuses beyond budget (`FLX-5010`).

## 5. Verifier (`concordance/verifier.py`, `ClearStatistic` backend)

Inputs: the contract set (recomputes `H`, `G`), `S`, `C1`, `C2`, and D′ (recomputes D′'s statistics on the same grid and D′'s digest = `outputDigest`). Evaluates each property in the integer cross-multiplied form:
- mean: `|Sx·n′ − Sx′·k| ≤ τ′·k·n′` (absolute) or the relative homogenised form;
- total variation: `Σ_bins |n′·c − k·c′| ≤ 2·τ′·k·n′`;
- level histogram: the same form over the declared levels, `Σ_levels |n′·c − k·c′| ≤ 2·τ′·k·n′`, with `ε = 0`;
- correlation: `L·R ≤ A ≤ U·R` split by signs, with `Qx ≥ k²·v`, `Qy ≥ k²·v`;
- quantile: order-statistic rule on both sides; with bins (`quantile/2`), the rule read at the reference bin's edges, `binBelow < j ≤ binThrough` with `j = ⌈level·k⌉`, the synthetic order statistic's own ranks, and `e_i − τ′ ≤ w′ ≤ t_i + τ′` for that statistic `w′`, where `t_i` is the bin's largest grid value (`e_{i+1} − 1`, or the last edge for the last bin);
- support: count of non-members ≤ `max`.
Fail-closed conditions: `k = 0`, count outside `records`, group below `records.min`, a value outside its range or its declared support, a value that needs more than 1000 digits written out (counted as §2 counts a declared number, and refused before it is formed), a column absent, `ε ≥ τ`. **No proof is generated in v0.1**; the `ProofSystem` port has one implementation, `none`, and the record says so.

Each comparison above is one registered statistic definition (`flux_stats`), so a property's reference can
also come from a profile (PROFILE-SPEC.md §7) and be compared in the same integer form. There a group or a cell the
profile suppressed is censored, never scored as zero, and a profile that holds no statistic of the property's shape
is refused (`FLX-5037`). The record of §6 is written for a committed state only. `verify` and `ship` score the
profile path's outcomes in a scorecard per property, table, dimension and overall (SCORECARD-SPEC.md).

## 6. The fidelity record (`concordance/record.py`): one per verified emit, in the evidence pack

```json
{
  "recordVersion": 1,
  "reference": { "productRef", "exposeId", "fluidVersion", "contractDigest" },
  "H": "sha256:…", "G": "sha256:…",
  "committedState": { "ordinal", "C1", "C2", "recordCount" | "recordRange", "committedAt", "checkOrdinal" },
  "synthetic": { "outputDigest", "rows", "generatorDigest" },
  "properties": [ { "property", "column(s)", "groupBy?", "tolerance", "margin", "epsilon", "outcome": "within|outside" } ],
  "proofSystem": "none",
  "scope": {
    "certifies": "statistical relation between the synthetic dataset and the committed state under the registered properties",
    "doesNotCertify": ["privacy or re-identification resistance of the synthetic dataset", "fairness or any property of the source dataset itself", "any property of a system trained on or tested with the synthetic dataset", "that the synthetic dataset was generated independently of the source", "that the committed state is the whole source dataset", "predictive validity of any simulation outcome"]
  },
  "columnsCovered": [...],
  "signature": { "alg": "ed25519", "keyId", "sig" }
}
```
The scope statement is generated from a fixed template, never hand-edited; its `doesNotCertify` list is the
register-discipline guard. The record is signed by the verifier key (ed25519 via `cryptography`), and travels in the
evidence pack under `run/fidelity-record-<exposeId>.json`.

## 7. The gate (`concordance/gate.py`, invoked by `fluxsim ship`)

Recompute `H` over the contract set; verify the record's binding to `H` and its signature; recompute D′'s digest and
statistics from the rows actually placed in the pack; re-evaluate every property; check `committedAt` against
`budget.maxStateAge`. On any failure: write no bundle, exit 1 with `FLX-8001`, emit a CloudEvent
`com.agentics.flux.ship.withheld` to the configured sink (stdout NDJSON by default). On success: pack (staged copy;
the `.fluid.yml` copied byte-for-byte) and print the public re-verification commands.

When the run's plan pins a profile, `ship` takes it with `--profile` and compares every emit's properties with it as
well (PROFILE-SPEC.md §7). An evaluation that is not within withholds the emit, and so does a missed scorecard
threshold or a measured score below the emit's `minCredibility` (SCORECARD-SPEC.md §5). With a committed slice too,
the emit ships only when both references find it within. The comparison with a profile writes no record; the
scorecard it feeds travels in the pack as `run/scorecard.json`.

## 8. What a change does

| Change | Effect |
|---|---|
| tolerance / `minVariance` / `maxStateAge` only, or the tolerance of a fact | `H` moves, `G` unchanged → re-run the check against the cached `S`, new record |
| any other declaration in the block or the contract | `G` moves → new committed state required (`FLX-5011`) |
| generator (engine `kernelVersion`) | `generatorDigest` moves → re-materialise D′, new record against the same state |
| appended source records | new committed state (ordinal+1); additive quantities folded, quantiles recomputed |
