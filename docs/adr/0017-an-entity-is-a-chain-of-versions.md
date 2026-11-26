# ADR 0017: an entity is a chain of versions: two counted layouts, chained validity before a fixed horizon, tracked columns and their transitions, `previous`, as-of links, and a window that grows without rewriting

## Status

Accepted 2026-11-22. It nests the counted layout of [ADR 0016](./0016-children-per-parent-are-counted.md)
and changes nothing in it, extends the parent reads of [ADR 0022](./0022-rows-read-their-linked-rows.md) to
versions, draws a first start and a version's length from a histogram as EXPR-SPEC-2 section 6 draws a value, and
keeps [ADR 0018](./0018-module-facts-live-in-the-flux-bundle.md)'s rule that module facts
live in the FLUX bundle. Its projections (a snapshot, change events, periodic rows) are
[ADR 0027](./0027-a-history-projects-snapshots-events-and-periods.md). Everything below generates under kernel
`0.2.0` only.
**Amended 2026-11-23:** profile format `1-3-0` and request format `1-1-0` shipped in 0.10.0.

## Context

FLUID 0.7.7 declares a type II slowly changing dimension field for field as dbt does: two time dimensions carry
`typeParams.validityParams`, one with `isStart` and one with `isEnd`, and one `natural` entity names the key whose
versions the window separates. The engine reads that declaration (`flux_engine/seam/keys.py`, the `versioned`
expose and its instant keys) and generates nothing from it:

- **One version per key.** A versioned expose is laid out as any expose: each row is drawn on its own, so a
  natural key's rows are not a chain. Two rows of one entity can overlap, an entity can have no current row or
  several, and its start and end come from their own statistics.
- **`previous` is not generated.** EXPR-SPEC-2 section 3 defines `previous.<column>` on an expose whose entity
  has versions: the version whose validity starts latest before this one. A FLUX-side rule that reads it is
  `FLX-6210`.
- **No as-of read.** A path through a relationship whose target declares a validity window is `FLX-6106`
  (EXPR-SPEC-2 section 3), so an invoice cannot read the plan its customer held on the invoice's date.
- **Nothing ties versions to their statistics.** A profile can say how many versions an entity has, how long a
  version lasts and how the tracked columns move from one version to the next; none of it reaches generation.
- **A window that grows rewrites the past.** A date with no declared range is drawn uniformly inside
  `spec.timeBounds` (SEAM-FIDELITY-SPEC section 1), so moving the window's end moves every such date. A history
  laid out to fit the window would be laid out again by every extension, and a dataset grown period by period
  needs its earlier rows to keep their bytes.

Borrow-before-build search, 2026-11-22. The codebase came first: ADR 0016's counted layout already gives exact
counts from a shape, a minimum, eligible parents, expected counts by a rank coupling and consecutive siblings;
`flux_engine.seam.keys` and `flux_engine.expr2` already resolve a versioned expose and `previous`; EXPR-SPEC-2
section 6 already draws a value from a histogram inside an interval. Searches: "dbt snapshot dbt_valid_from dbt_valid_to dbt_valid_to_current
hard_deletes new_record dbt_is_deleted", "dbt-utils mutually_exclusive_ranges zero_length_range_allowed gaps
not_allowed lower_bound_column", "synthetic data generator slowly changing dimension type 2 history versions
valid_from valid_to open source python". Fetched dbt's snapshot, `hard_deletes` and `dbt_valid_to_current`
pages, and dbt-utils' README and `macros/generic_tests/mutually_exclusive_ranges.sql` at the tag `1.4.1`.
Versions and licences were read from PyPI and GitHub on that day.

| Name | What it does here | Licence | Taken |
|---|---|---|---|
| `flux_engine.generator.counts` (this repository) | `counted_layout`: counts from a shape, a minimum, eligible parents, expected counts by a rank coupling; `CountedLayout.parent_of`, `parents_of`, `children_of` over consecutive siblings | Apache-2.0 | reused twice, one layout inside the other: entities per parent, versions per entity |
| `flux_engine.core.lrm`, `flux_engine.core.permute` (this repository) | the Largest Remainder Method on exact integers; `permute/2` | Apache-2.0 | reused: which entities end, which change moves which column, a chain compressed to its horizon |
| `flux_engine.seam.keys`, `flux_engine.expr2` (this repository) | a versioned expose's natural entity and validity dimensions; `previous` resolved and evaluated | Apache-2.0 | reused: the history reads its window from the contract, and `previous` generates |
| EXPR-SPEC-2 section 6 (this repository) | a value drawn from a histogram, inside an interval, with one `uniform53` at a column's unit | Apache-2.0 | reused: a first start and a version's length |
| FLUID 0.7.7 `validityParams` and the `natural` entity | dbt's validity params field for field: a model with a window has exactly two such dimensions and one natural entity | Apache-2.0 | the declaration a history reads, never repeated in the bundle |
| dbt-core 1.12.7 snapshots (docs for 1.9 and later; 2.0.0rc9 is a pre-release) | `dbt_valid_from`, and `dbt_valid_to` NULL on the current row or `dbt_valid_to_current`'s sentinel such as `9999-12-31`; each run closes the old row at the new row's start; `hard_deletes`: `ignore`, `invalidate` (the last row is closed), `new_record` (a row with `dbt_is_deleted` true) | Apache-2.0 | the semantics of the chain (`nextStart`), the open end (`openEnd`) and the two end modes: `closeLast` is `invalidate`, `finalVersion` is `new_record` |
| dbt-utils 1.4.1 (2026-06-28), `mutually_exclusive_ranges` | per `partition_by`, ordered by lower then upper bound: lower below upper unless `zero_length_range_allowed`; under `gaps: not_allowed` each upper bound equals the next lower bound; the last record passes | Apache-2.0 | the definition of `no_gaps_no_overlap`: `gaps: not_allowed`, zero length refused, per entity |
| PostgreSQL 18 temporal foreign keys (ADR 0024's receipt, same day) | the referenced history covers the referencing row's time | PostgreSQL Licence | the semantics of an as-of read |
| Databricks AUTO CDC, SCD type 2 (docs) | `__START_AT` and `__END_AT`, NULL on the active row | proprietary documentation | confirms the open end; pattern only |
| dbldatagen 0.4.0.post1 (the Databricks notebook "tws scd2 python") | rows for an SCD type 2 pipeline; the pipeline, not the generator, makes the versions | Databricks License | none |

No tool found lays an entity's versions out from a declared number of versions per entity, keeps every version
before a horizon without reading the window's end, or makes a dated row read the version valid on its date. No
new dependency is needed.

## Decision

### 1. Kernel 0.2.0 only, and only where a bundle asks

The namespace gains two keys the engine did not read before: `history` and `asOf`. A bundle that declares
neither generates, plans and digests exactly as before under both kernels: a versioned expose is laid out a row
at a time as today, `previous` stays `FLX-6210` to generate, and a path into a validity window stays `FLX-6106`.
Under kernel `0.1.0` each key is refused with `FLX-3105`, naming the kernel. A `history` entry changes its
emit's rows, so it is bound into that emit's entry for `H`. An `asOf` entry changes its reading emit's rows, so it
is bound into the reader's entry for `H`. The reader's plan document carries the history's plan as ADR 0022
section 3 carries every plan a path reaches; a plan document holds the window, so it enters `planDigest` and
never an entry for `H`, whose every member the seed tree's root folds in. `G` leaves both keys out, as it leaves
out `links`.

### 2. The `history` entry

The namespace key `history` lists one entry per versioned emit:

```yaml
history:
  - emit: subscription_version
    entities: 1000
    parent: { relationship: customer, minChildren: 1, children: { levels: ["1", "2", "3"], levelWeights: ["0.70", "0.20", "0.10"] } }
    versions: { bins: ["1", "2", "4", "9"], binWeights: ["0.45", "0.35", "0.20"] }
    start: { bins: ["2023-01-01", "2024-01-01", "2025-07-01"], binWeights: ["0.55", "0.45"] }
    length: { bins: ["30", "90", "365", "1096"], binWeights: ["0.30", "0.45", "0.25"], closed: lower }
    until: "2025-12-31"
    chain: nextStart
    activation: started_on
    tracked:
      - column: plan
        transitions: { basic: { pro: "0.8", team: "0.2" }, pro: { basic: "0.5", team: "0.5" }, team: { pro: "1" } }
    end: { mode: finalVersion, share: "0.12", column: status, value: cancelled }
```

- `emit` names an emitted expose whose contract declares a validity window: one natural entity and two time
  dimensions with `validityParams`, `isStart` and `isEnd`, both DATE or both TIMESTAMP. The entity, the start and
  the end are read from the contract and never repeated in the bundle.
- `entities`, a whole number above zero: the number of entities, scaled by `--scale` as `rows` is. The emit's
  `rows` is its number of versions.
- `parent`, optional: the relationship of the emit whose parent holds each entity, with three of the keys a
  `counts` entry reads (ADR 0016 section 1): `minChildren`, `parentWhen` and `children`, read as there, over
  entities instead of rows.
- `versions`, optional: the shape of the number of versions per entity, `levels` with `levelWeights` or `bins`
  with `binWeights` over whole counts of at least 1, in ADR 0016's spelling of `children`. Without it the
  versions are spread over the entities as evenly as whole counts allow (section 3).
- `start`, optional: the histogram of an entity's first start, `bins` and `binWeights` over values of the start
  column's type, with its edges inside `[timeBounds.start, until]`. The first start is drawn from its part inside
  section 4's interval `J`, and without it uniformly over `J`.
- `length`: a version's length, a histogram whose edges are whole days of at least one, written as EXPR-SPEC-2
  section 6 writes a `distribution` (`bins`, `binWeights`, `closed`). A length is drawn at the chain's unit
  (section 4), so a TIMESTAMP chain's lengths are whole milliseconds inside its day bins. No other spelling is
  read: an `open` weight, a `mean`, or a family's `given`, `states` and `otherwise` are `FLX-3105`.
- `until`, required: the history's horizon, a value of the start column's type. Every version starts before it,
  every closed version ends before it, and the last version of an entity that has not ended is open at it.
  It is the instant the history is laid out to, fixed in the bundle, so that the window can grow past it
  (section 10).
- `chain`: `nextStart`, the default, or `dayBefore` (section 4).
- `openEnd`, optional: the text written in the end column of an open version, read as the column's type, such
  as `"9999-12-31"`. Without it an open end is NULL.
- `activation`, optional: a column of the start's type that holds the entity's first start on every version.
- `tracked`: the columns that change between versions, each `{column, initial, transitions}`, and `reasons`, the
  share of changes each tracked column makes, `levels` naming tracked columns with `levelWeights`, required when
  more than one column is tracked (section 6).
- `end`, optional: how an entity's history ends, `{mode, share, column, value}` (section 5).

No other key is read; an unknown key is `FLX-3105`. Refused at plan time with `FLX-3403`: an emit without a
validity window, or with a start and end of different types; a second entry for one emit; a `parent` that is not
a to-one relationship of the emit, that a `counts` entry also names, or that a `links` entry gives a `when` or a
`weight` (ADR 0016 section 1 gives the reasons); a `versions` class below 1; an `until` outside the window
(section 11); a `start` bin outside `[timeBounds.start, until]`; a `length` bin edge below one day;
`dayBefore` on TIMESTAMP columns; an empty first-start interval `J` (section 4); a primary key other than one
column, or than the natural entity's columns and the start (section 3); a natural entity whose column is also a
foreign key of the emit, whose key would come from its parent rather than from the entities' layout; an `openEnd` on a nullable end column that is not the column's type, or
no `openEnd` on an end column the contract requires; an `activation`, tracked or end column that is a key, a
validity column or a column a rule sets; and every refusal of sections 5, 6 and 8. Every refusal reads declared
values, the scale and the layout's counts, which follow from declared values alone, never a drawn row. A
`lifetimes` or `within`
entry (ADR 0024) on the start, the end or the activation column of a history's emit is two sources for one
value, `FLX-6203`, except the `within` entry that says `parent.lifetime: true` (amendment of 2026-11-26).

### 3. Entities per parent and versions per entity: two counted layouts, nested

The history lays its emit's rows out with the counted layout twice, one inside the other. It calls
`flux_engine.generator.counts.counted_layout` as it stands and changes nothing in it (KERNEL-SPEC-2 section 8.1):

1. **Entities per parent.** With a `parent`, the entities are the children and the parent's rows the parents:
   `A = counted_layout(K_A, P, E, shape, minimum, eligible, expected)`. `K_A` is the key, by KERNEL-SPEC-2
   section 2, of the path `("sim", sim_id, "gen", exposeId, "entities", relationship)`; `P` is the parent's
   rows and `E` the scaled `entities`; the shape, the minimum and the eligible parents are the `parent` entry's,
   read as a `counts` entry's. An expected-count hook that returns a member for the relationship (ADR 0016
   section 3) gives `expected` as it would for a `counts` entry. Without a `parent`, entity `e` has no parent and
   the entities' order is their index.
2. **Versions per entity.** The versions are the children and the entities the parents:
   `V = counted_layout(K_V, E, N, versions, 1, None, expected)`. `K_V` is the key of
   `("sim", sim_id, "gen", exposeId, "versions")`; `N` is the emit's scaled rows; the minimum is `1`, since an
   entity without a version is not in the table. With a shape, `expected` is each entity's room, `until` less
   its first start (section 4), as an exact whole number of the chain's units: ADR 0016 section 3's expected
   counts, so the slot values go to the entities in the order of their room, the entity with the most room
   taking the most versions (the rank coupling). Without a shape, `expected` is none, so every entity counts
   alike and takes `floor(N / E)` or `ceil(N / E)` versions. The room reads drawn first starts and `until`,
   never the window.

Version row `i` is version `j = i - V.offsets[e]` of entity `e = V.parent_of(i)`, and entity `e` is a child of
parent row `A.parent_of(e)`. An entity's versions are consecutive rows and so are a parent's entities, so a
parent's versions are one run of rows too, in parent order, then entity order, then version order. That is what
`previous`, the as-of read and ADR 0027's projections read without a pass over the table. More entities than
versions is ADR 0016's `S < 0` refusal (`FLX-3403`), raised at plan time and naming `--scale` under a scale.

**The counts are known at plan time.** Which entity takes which count follows the room, but the counts
themselves, as a multiset, follow from `N`, `E` and the shape alone, so `c`, the largest count an entity takes,
is worked out at plan time from declared values; section 4 reads it. **Scale.** `--scale` keeps at least one entity, one row and one parent
row, so every history has an entity; more entities than versions is the refusal above.

- **The entity's key.** The natural entity's columns are laid out over the entities as KERNEL-SPEC section 8.2
  lays a key out over rows, the entity index standing for the row index, and every version copies its entity's.
  So the check that a key's declared range holds its rows counts the entities, `E`, for a history's emit.
- **The parent's key.** The `parent` relationship's foreign key copies parent row `A.parent_of(e)` whole on
  every version of `e`, as a counted pick does, and its column draws nothing.
- **The version's key.** The primary entity, the key of one version, is either one column, laid out over the
  rows as today and unique per version, or the natural entity's columns and the start: the natural columns from
  the entity's key, the start from the chain, unique because starts increase along a chain. Any other primary
  key is `FLX-3403` in this version.

### 4. The validity chain

An entity's versions are a chain drawn on its own streams, and nothing in it reads `spec.timeBounds.end`. The
chain's unit is a whole day for DATE columns and a millisecond for TIMESTAMP columns.

1. **The first start** `a` is drawn at position 0 of `("sim", sim_id, "gen", exposeId, "history", entity)`,
   at the chain's unit, inside `J = [timeBounds.start, until - c')`, where `c'` is `c` (section 3), plus one
   under `closeLast`, in units: from the part of `start` inside `J` with one `uniform53`, as EXPR-SPEC-2
   section 6 draws a value inside an interval, or uniformly over `J` with one `bounded` at the same position
   when `start` is absent or no bin meets `J`. Which of the two a row takes depends on `J` and the bins, never
   on a draw. So every entity has at least `c' + 1` units of room. An empty `J` is `FLX-3403` at plan time.
2. **Lengths.** Version `j`'s length `d_j` is drawn with one `uniform53` at position 0 of
   `("sim", sim_id, "gen", exposeId, "change", entity, j)`, as EXPR-SPEC-2 section 6 draws a value from
   `length`, in whole units of at least one. Every version draws its length, the last included; the length of an open version is
   left unread, so no position depends on the count.
3. **Starts.** `s_0 = a` and `s_(j+1) = s_j + d_j`.
4. **The fit.** Every start and every closed end lies before `until`. Let `k` be the entity's closed versions
   and `D` the sum of their lengths. When `D > room - 1`, the closed lengths are compressed: each keeps one unit,
   and the other `room - 1 - k` units are allotted over them in proportion to `d_j - 1` by the Largest
   Remainder Method on exact integers, ties to the lower version. Since `k` is at most `c'` and the room at
   least `c' + 1` (item 1), every chain fits, so no drawn row is ever refused. The compression reads `until`
   and drawn values only, and draws nothing. Since the rank coupling gives the
   most versions to the most room, a chain is compressed only where its drawn lengths overrun its horizon.
5. **Ends.** Under `nextStart` version `j` ends at `s_(j+1)`: a half-open interval, as dbt snapshots and
   PostgreSQL ranges write it. Under `dayBefore` it ends the day before `s_(j+1)`: a closed interval, for DATE
   columns only, in which a one-day version, or a compressed one, starts and ends on the same day. The last version of an entity that has not ended is open: its end is NULL, or `openEnd`. So
   each entity has one open end, at `until` and so inside the window.

**A length is a histogram in this record.** Positions 1 to 255 of the `change` path stay reserved, so that a
later record can draw a length another way there, given the version's state, without moving any position
(section "What would change this decision").

### 5. End modes and activation

**Activation.** The `activation` column holds `s_0`, the entity's first start, on every version.

**How a history ends.** With an `end`, a share of the entities have ended by `until`:

- `share`, a decimal from 0 to 1: the ended entities are `n = LRM(E', [share, 1 - share])[0]` of the `E'`
  eligible entities, keyed `"0"` and `"1"`, and the entity of eligible rank `r` ends when
  `permute/2(K_end, E', r) < n`, where `K_end` is the key of `("sim", sim_id, "gen", exposeId, "ended")`. So the
  share is exact up to the largest remainder, and which entities end is the permutation's, not a draw's.
- `column` and `value`, optional: an ended entity's last version is its ending version and takes `value` in
  `column`, a categorical column of the emit, tracked or not. Its change is forced, not drawn, and is no
  change reason (section 6). `value` may appear nowhere else: a tracked `column` whose `initial` or whose
  transitions give `value` a weight above zero, or a carried one whose statistics do, is `FLX-3403`. So the ended
  status sits on the last version only.
- `mode: closeLast`, dbt's `invalidate`: an ended entity's last version is closed, with its own drawn length
  inside the fit, at `s_last + d_last` under `nextStart` and at the day before, `s_last + d_last - 1` day, under
  `dayBefore`. It has no open version. Every entity is eligible, or with a `column` every
  entity with at least two versions.
- `mode: finalVersion`, dbt's `new_record`: an ended entity's last version is a final version that carries
  `value` and stays open, so every entity keeps one open version. It needs a `column` (`FLX-3403` without one),
  and the eligible entities are those with at least two versions.

`E'` comes from the layout, so the count is known at plan time. With no eligible entity, as when a small `--scale`
leaves every entity one version, none ends.

### 6. Tracked columns, change reasons and carried columns

- **Tracked columns.** Each entry names a categorical column of the emit (it declares `levels` and has no
  `nullFraction`, else `FLX-3403`). Version 0 takes its `initial` value, `levels` with `levelWeights`, or by
  default the column's own levels and weights. `transitions` maps each level to the weights of the levels it
  moves to, never itself. Every level a chain can reach needs a row whose weights total above zero, else
  `FLX-3403`.
- **One reason per change.** Each later version changes exactly one tracked column, its change reason, so a
  version always differs from its previous one. The ordinary changes, every version after an entity's first
  except an ending version, are numbered `t` in row order, `T` of them. With one tracked column it is the reason
  of every change. Otherwise `reasons` gives the classes, `LRM(T, weights)` changes per class keyed as ADR 0016
  section 6 keys them, and change `t` takes the class of slot `permute/2(K_reason, T, t)` laid out class by
  class, `K_reason` the key of `("sim", sim_id, "gen", exposeId, "reason")`. So each tracked column makes its
  share of the changes exactly, and the count per reason is known at plan time without a draw.
- **The new value.** The reason's column moves from its value on the previous version to a level drawn from
  that level's row of `transitions`, one `categorical` at position `256 + c` of the version's `change` path,
  `c` the column's place in `tracked`. Version 0 draws each tracked column's initial value at its own
  `256 + c`. The other tracked columns carry their previous value.
- **Carried columns.** Every other column of the emit that the history does not set (not the entity's key, the
  version's key, the parent's key, a validity column, the activation, a tracked column or the end column) and
  that no rule sets takes, on every version, the value its own cell draws on the entity's first version row.
  The later rows' cells are never drawn. A foreign key other than the parent's is picked on the first version
  row and carried too. A carried column drawn `given` a tracked column is `FLX-3403`, since its value would have
  to change with a column that changes.
- **No exact allocation.** A column of a history's emit under `allocation: exact` is `FLX-3403`, since its values
  follow the entity, not the row.
- **Rules** run on each version row after these values, as ADR 0022 orders them, so a contract `derive` over a
  tracked column, such as a price from the plan, follows the version.

### 7. `previous`

On a history's emit, a FLUX-side rule that reads `previous.<column>` or `previous.<relationship>.<column>`
(EXPR-SPEC-2 section 3) generates. The previous version of row `i` is row `i - 1` when `i` is not its entity's
first row; on the first version every `previous` path is NULL. The rule reads the previous row's final value, its
own rules included, and an entity's rows are made in order, so a chain of `previous` reads holds version by
version. A versioned emit with no `history` entry keeps the refusal, and an unversioned expose keeps `FLX-6106`.
A rule that reads both `previous` and a window over siblings is `FLX-6210` in this version, since each would
wait on the other.
`fluxsim verify` evaluates such a rule on the output by the same definition, the previous version being the
entity's row whose start is latest before this one's.

### 8. As-of links

The namespace key `asOf` lists `{emit, relationship, date}` entries. `relationship` is a to-one relationship of
`emit` whose target is a history's emit and whose `toColumns` are that history's natural entity; `date` is a DATE
or TIMESTAMP column of `emit`. Anything else is `FLX-3403`.

- **The read.** A path through `relationship` reads the version of the linked entity valid on the row's `date`:
  the one whose start is not after the date and whose end is open or, under `nextStart`, after it, or, under
  `dayBefore`, not before it. Between a DATE and a TIMESTAMP, a DATE is the instant of its midnight UTC, with
  one exception: under `dayBefore` a DATE end covers its whole day, up to the next midnight UTC. So under
  `nextStart` a TIMESTAMP on the day a version starts belongs to that version only, and a DATE `date` read
  against a TIMESTAMP history reads the version valid at that date's midnight UTC. The version is found by
  bisection over the entity's starts, which its chain gives without reading the table.
- **No version.** A row dated before its entity's first start, or after an ended entity's closed end, reads NULL
  through every path of the link, as a link that reaches no row does. `fluxsim verify` reports how many rows
  read no version on an `asOf:<relationship>.<date>` line, measured and not scored, since the date is the row's
  own and this record does not move it.
- **A required column.** A column the reader's contract does not leave nullable, set by a rule that reads
  through the link, is `FLX-3403` at plan time, since a row that finds no version would write NULL there;
  unless the history has no `end` and the declared range of `date` starts at or after `until`, so that every
  row finds a version.
- **Without an entry**, a path into a history's emit may read the entity's carried columns, its key and its
  activation, which are the same on every version. A tracked column, a validity column or a column a rule sets
  stays `FLX-6106`, and the message names `asOf`.
- **The pick** is unchanged: a child picks a row of the history's emit as any child does, so an entity is picked
  in proportion to its versions, and its foreign key copies the entity's key. Which version a path reads is the
  date's, not the pick's. A `distinct` or `one_to_one` relationship into a history's emit, and a `counts` entry
  on one, are `FLX-3403` in this version.
- **Order.** `date` is ordered before every rule that reads through the link. A `date` set by a rule that reads
  through the same link is a cycle, `FLX-6208`.

`fluxsim verify` checks a rule that reads through the link with the same as-of join on the run's rows.

### 9. Paths, positions and vectors

| path | kind | holds |
|---|---|---|
| `("sim", sim_id, "gen", exposeId, "entities", relationship)` | layout key | entities per parent (section 3) |
| `("sim", sim_id, "gen", exposeId, "versions")` | layout key | versions per entity (section 3) |
| `("sim", sim_id, "gen", exposeId, "history", entity)` | stream, one ordinal | position 0: the first start |
| `("sim", sim_id, "gen", exposeId, "change", entity, version)` | stream, two ordinals | positions 0 to 255: the length, 0 for one `uniform53`, 1 to 255 reserved; `256 + c`: tracked column `c`'s value |
| `("sim", sim_id, "gen", exposeId, "ended")` | permutation key | which entities end (section 5) |
| `("sim", sim_id, "gen", exposeId, "reason")` | permutation key | which change moves which tracked column (section 6) |

KERNEL-SPEC-2 section 2 gains two ordinal parts, the entity index and the version index, so the `history` path's
counter is `(entity, 0, draw, attempt)` and the `change` path's `(entity, version, draw, attempt)`. A layout or
permutation key is the full 32-byte key of its path, which has no ordinal, as ADR 0016's `count` key is.

**Additive** under KERNEL-SPEC-2 section 12. Each shape has, at the place after `exposeId`, a literal that no
other shape has there (`entities`, `versions`, `history`, `change`, `ended` and `reason`, beside the existing
`row`, `key`, `pair`, `count` and `allocate`), so every address is new. The paths are read only for a bundle that
declares `history`. Nothing that exists moves: the first version's cells draw on the generator cell path as they
always did, the other versions' cells are never drawn, which moves no draw since every cell has its own counter,
and the parent's foreign key draws nothing on its own stream, as on ADR 0016's layout. The vector file
`tests/vectors/kernel2-history-vectors.json` pins each new path's key, counters and blocks, and the layouts,
chains, fits, ended entities and reasons of chosen entries. The layout and permutation keys stay out of the
seed-path ledger, as ADR 0016's do.

### 10. A window that grows appends and never rewrites

Extending `spec.timeBounds.end` keeps every row a history made before, byte for byte, because no draw and no
value of a history reads the window's end:

- the layouts read the scaled `entities` and `rows`, the parent's rows and the drawn first starts;
- no plan document, which holds the window, enters an entry for `H`, and the seed tree's root folds in every
  `H`, so the root and every stream are the same under both windows;
- every draw is addressed by an entity and a version, never by the window, and a history's emit keeps its row
  indices since its layout does not move;
- the fit, the chain and the open end read `until`, which the bundle fixes, and `until` lies inside the window
  (section 11), so a longer window still holds every version;
- a DATE or TIMESTAMP column of a history's emit that declares no range would be drawn inside the window and
  move with it, so it is refused (section 11).

So a history's emit, and every column a rule computes from it, is the same after an extension. What grows is
what ADR 0027 lays out by period: the periods after the old end, appended. Growing the history itself, with
changes after `until`, means moving `until`, which lays the chains out again; section "What would change this
decision" says how that could append too.

Two tests hold this. One runs a bundle under two windows with the same `until` and compares the history's emit
byte for byte, and its digests; the bundle declares a history with no `versions` shape and one with a shape,
the default `start`, an entity whose chain is compressed, a DATE and a TIMESTAMP chain, both end modes, an
`asOf` reader and a projected emit. The other works a history out with a `timeBounds` whose end raises when it
is read.

### 11. Dates outside the window

`fluxsim validate` reports a DATE or TIMESTAMP range or bin outside `timeBounds` as a warning today. On a
history's emit and on every emit ADR 0027 projects from it, the warning becomes a refusal, `FLX-3403`, and two
more refusals join it:

- the history's own dates: `until` after `timeBounds.end` or not after `timeBounds.start`, a `start` bin edge
  outside `[timeBounds.start, until]`;
- a DATE or TIMESTAMP column of these emits that declares no range and would be drawn inside the window, since
  its rows would change when the window does.

`openEnd` is the one date allowed outside the window, since it marks an open end rather than an instant. Every
other emit keeps the warning, so no bundle written before this record is refused.

### 12. Profile format 1-3-0 and request format 1-1-0

Both were unreleased when this record was written and shipped in 0.10.0. They gain optional members rather than new versions:

- **Profile, `tables.<id>.history`**, for a table with a validity window: `versionsPerEntity`, a
  `marginalHistogram/1` of the entities by their number of versions; `firstStart`, a `marginalHistogram/1` of the
  entities' first starts; `versionLength`, a `marginalHistogram/1` of the days from a version's start to the
  next version's start, over the versions that have a next one; `chain`, a `levelHistogram/1` of consecutive
  versions over `["nextStart", "dayBefore", "gap", "overlap"]`; `lastVersion`, a `levelHistogram/1` of the
  entities over `["open", "closed"]`; and for each candidate tracked column, `initial`, a `levelHistogram/1` of
  its value on first versions, and `transitions`, a `levelHistogram/1` of its next value grouped by its value
  (`groupBy`), over the changes where it moves; and `reasons`, a `levelHistogram/1` of the changes by the one
  tracked column they move, with `several` for a change that moves more than one. Every member reuses a
  statistic kind the reader already knows, with its disclosure rules and noise.
- **Request, `tables[].history`**: for each expose with a validity window, the members above, bounded, with its
  categorical columns other than keys as the candidate tracked columns. A request that asks for no history is
  written as before.
- **flux-stats API**: `TableEntry.history` and its reader.
- **No scorecard change.** The lines of section 14 and the `asOf:` line of section 8 are `rules` lines, so
  scorecard format `1-4-0` keeps its members, as ADR 0027 section 8 says of its own checks.
- The request member extends request format `1-1-0`, which ADR 0024 defines.

### 13. `fluxsim init`

For each expose with a validity window whose profile table publishes `history`, `init` writes a `history` entry:
`entities` from the histogram's total and `versions` from its classes, as ADR 0016 section 7 writes `children`;
`start` and `length` from their histograms; `until` as the window's end it writes; `chain` as the member of
`chain` that holds on at least 90 % of consecutive versions, the constant `CHAIN_HOLDS_AT_LEAST`, or no entry,
recorded as unused because the versions do not chain, when neither does; `tracked` as each candidate column that
moves in at least one published change, with its `initial` and `transitions`, and the others carried;
`reasons` from their histogram, without `several`; `end` as `closeLast` with the closed share of `lastVersion` as
its `share`, when that share is above zero. `init` writes no `parent`: a profile publishes children per parent
over rows, which are versions here, and no relationship of the contract says which parent holds an entity, so a
foreign key stays a carried column (section 6). It writes no `finalVersion`, since a profile does not say which
value marks an end. For each relationship into such an expose from an expose that declares a
`defaultAggTimeDimension`, it writes an `asOf` entry on that dimension. Every published member it does not use is
recorded as unused with its reason, as ADR 0014 requires.

### 14. Checks

`fluxsim verify` checks each history on the run's rows, each line enforced, so one violating entity fails the
run, as a `derive` without a band does:

- `history:no_gaps_no_overlap`: per entity, versions ordered by start then end. Under `nextStart` every start
  is before its end and every end equals the next start: dbt-utils' `mutually_exclusive_ranges` with
  `partition_by` the entity, `gaps: not_allowed` and no zero length. Under `dayBefore` every start is not
  after its end and every end is the day before the next start: the same test on closed intervals.
- `history:one_current`: an entity that has not ended has exactly one open version, its last; one ended under
  `closeLast` has none; one ended under `finalVersion` has one, its final version.
- `history:ended_is_last`: with an `end` column, `value` appears only on an entity's last version.

A history on a hidden table (ADR 0024 section 3) is listed as not scored, with that reason.

## Consequences

- A bundle that declares no `history` and no `asOf` keeps every byte and every digest under both kernels. A golden
  pins a bundle whose contract declares a validity window, with the digests origin/main gives for it, and the
  same bundle with a history, whose rows move.
- Each process that writes a slice of a history's emit works out both layouts, which reads every entity's first
  start, and the chains of the entities in its slice: a cost that follows the entities, paid once per process.
  `--jobs 1` and `--jobs 4` give the same digests, and the shards of `--shard I/N` together give the whole run.
- Kernel `0.2.0`'s column path takes a history a column at a time: carried columns from their first version
  rows, tracked columns and validity entity by entity. A test per construct compares its rows with the per-row
  path's.
- Tests cover kernel `0.2.0`'s column path against the per-row path for each construct, `--jobs 1` against
  `--jobs 4`, `--scale` below 1, down to one version per entity, and `--shard I/N` with shares that cut an
  entity's versions.
- A history's emit comes out grouped by parent, then entity, then version, in row order, as ADR 0016's layout
  orders a child table. A consumer that needs another order sorts it.
- No entity's first start falls in the last `c'` units before `until` (section 4): the price of a fit that can never
  fail. A reader of the run's first starts sees that gap at the end of the horizon; it is the construction, not a
  defect of the draw.
- The history stops changing at `until`: an entity that has not ended keeps its last version open whatever the window,
  and a longer window adds periods to ADR 0027's projections, not versions.

## What would change this decision

- A dataset that must grow its history and not only its periods. Each chain could continue past `until` on
  version ordinals beyond the layout's count, drawn on the same `change` path, so the versions before `until`
  keep their bytes; the number of versions after `until` would then follow the dwell draws rather than the
  layout, and the change reasons would need an allocation per period rather than one over all changes.
- A profile that publishes how far the number of versions follows an entity's room. The rank coupling makes the
  order perfect, a simplification, as ADR 0016 says of exposure.
- Changes that move several tracked columns at once, which the `several` reason counts: a joint transition
  over several columns would replace one reason per change.
- A tracked number column, which would move along bins instead of levels.
- A length drawn from a dwell family given the version's state, which would make the chain semi-Markov: drawn
  at positions 1 to 255 of the `change` path, which stay reserved for it, so no position moves.
- Picks per entity rather than per version, and `counts` entries over entities, for children of a versioned
  parent.
- A reader whose `date` is bound to its entity's validity by an ADR 0024 `within` entry, which would admit an `asOf`
  read into a required column beside the static condition of section 8, which is conservative.
- A satellite keyed by its hub's key: a natural entity that is also a foreign key, whose entities would take
  their parent's key, one entity per parent.
- A FLUID spelling for a history's generation facts, which would move them into the contract.

## Amendment of 2026-11-24: a history keeps to its parent's lifetime

A history's dates are absolute: its first starts are drawn inside `J` and its chains fitted to `until`, so they
ignore the lifetime of the parent row the `parent` relationship reaches. A vehicle could start before its
depot opened and outlive it. A `lifetimes` or `within` entry on the history's start cannot say it, since the
history sets that column (section 2, `FLX-6203`). This amendment is the history's counterpart of ADR 0024's `within`
with `lifetime: true` (section 5 there), and changes no bundle that does not ask for it.

**The key.** `history[].parent.lifetime: true`, default false, in the `parent` entry beside `minChildren`,
`parentWhen` and `children`. It enters the entry's member in `H` as the other members do, so a bundle without it keeps
every byte. It needs `parent.relationship` and a `lifetimes` entry (ADR 0024 section 4) on the emit that relationship
reaches: its `start` column, and its `end` column when it names one, are the lifetime the history keeps to. Without
the entry the history is refused at validation with `FLX-3403`, naming `parent.lifetime`; a `parent.lifetime` that is
not `true` or `false` is refused the same way. When the parent emit declares a history of its own, a lifetime
column that history does not carry (a validity column, the activation, a tracked or the end column, or one a rule
sets) can differ between the versions of one parent, so the bound would hold an entity to an arbitrary version: the
history is refused with `FLX-3403`, naming the emit and the columns. Kernel `0.2.0` only, as every history.

**The lifetime.** For an entity, `P = [ps, pe]` is the lifetime of its parent row, read in the chain's unit as
ADR 0024 reads a lifetime: a DATE start bounds a TIMESTAMP chain from its midnight, a DATE end up to the last
millisecond of its day, and `pe`, the parent's last day, is absent when the lifetime declares no end, the end is NULL,
or the end is before the start, so a parent that violates its own lifetime reads as open and cuts nothing. A parent
whose start is NULL bounds nothing. The lifetimes are the parents' final cells, read once per process at plan time
from the parent's own plan, a chunk at a time, and kept by the parent's digest, as a weighted pick reads its parent's
weights (ADR 0023 section 4). The plan grows with the parents' rows for a history that declares the key and for no
other. Which parent an entity has is the entity layout's (section 3), so that layout is worked out first, and the
first starts, rooms, version counts and ended entities after it.

**1. The first start.** The chain of entity `e` must end at `limit = min(until, pe)`, its parent's last day when that
is before `until`. The first start is drawn at `s` in `[max(a0, ps), limit - c' - 1]`, with `a0` the window's first
unit and `c'` as section 4 says (`c` and one more under `closeLast` or for a history with this key and no `end`,
since an entity whose parent ended has a closed last version, point 3), so the entity has the room its `c`
versions need. Where that interval is empty, the first start is drawn in `[max(a0, ps), limit - k - 1]`, with
`k = 1` and one more for a closing unit: the room for one version, the least an entity has. From the part of `start`
inside the interval with one `uniform53`, or uniformly over it with one `bounded`, at position 0 of the same
`history` path as before. Entities that share an interval draw by the same table, so an entity whose parent cuts
nothing draws what it drew without the key. An entity for which even the room for one version is empty is
`FLX-3403` at plan time, naming `history(<emit>).parent.lifetime`, how many entities it is and the range of their
parents' lifetimes: the bound is never written knowing it will be violated.

**2. The room and the counts.** Entity `e`'s room is `r = limit - s`, and its chain holds `r - z - 1` versions at most,
with `z` one when an ended entity's last version is closed (point 1) and zero otherwise: the first start and a closed last version each take a unit
(section 4, point 4), and by point 1 that is at least one. The versions per entity are assigned as section 3 says,
the rank coupling reading the rooms, and each entity's count is then capped at what its room holds. The versions
an entity loses go to other entities, one at a time, in rounds: each round gives one to every entity that is
still under its own cap, the entity with the most room first and a tie to the lower entity index, until none is left.
This is the cap-and-pass of `counts._capped`, the allotment of a `one_to_one` relationship, with a cap for each
entity in place of one (`counts.pass_excess` is both), so the emit keeps its declared rows and the shape's order is the
rooms' order. An entity that takes more than the shape's largest count does so only for the versions another entity
lost. Only when no entity has room left do the rows shrink: the history then has fewer rows than declared, the
shortfall. `plan`, `apply` and `verify` count the rows it has: the emit is planned again at them, and a layout is
always worked out from the declared rows. The shortfall is no refusal and is recorded: the emit's entry in `plan.json`
holds `declaredRows`, the rows the contract declares after `--scale`, and `shortfall`, the rows missing from `generator.rows`,
and only an emit whose shortfall is above zero has the two keys, so every other plan keeps its bytes and its run id.
`planDigest` covers them. `fluxsim verify` reads them back and prints one line for that emit, `shortfall <emit>: <rows>
rows written of <declaredRows> declared (<shortfall> short)` (`historyShortfalls` in `--json`), and no line for a run
with none. A history whose parent is itself such a history reads the
rows its parent holds, so it is laid out after the parent is planned at them, in as many passes as there are emits
and one; a count still moving after that is `FLX-3403`. Since the count never exceeds the room, the fit (section 4, point 4) never
fails and the chain ends by `limit`.

**3. The ending.** An entity whose parent's last day `pe` is at or before `until` is *forced* to end:

- under `finalVersion` its final version starts, at the latest, the day before `pe`, and carries `value`;
- under `closeLast` its last version closes, at the latest, the day before `pe`;
- with no `end`, its last version is closed at the latest the day before `pe`, and no other entity ends: the
  history closes the last version of the forced entities only.

A forced entity ends whatever its number of versions: with one version, that version is its ending version
(under `finalVersion` it takes `value`, and it is no change reason). Such an entity is in its ending state from its
first day: its only version starts at its first start and already carries `value`, so a column that says the entity
is ended reads so on every row it has. This is kept on purpose, since a rule that ties the version's state to the
parent's end (the entity is ended once its parent is) needs it, and `plan` says how many entities there are, one
note naming the emit, so the count is no surprise. A parent that ended after `until` bounds the room only, and forces
nothing.

**The share.** Forced entities can end whatever their versions and count towards the entities that end. As the
amendment below says, `n` is `min(LRM(E, [share, 1 - share])[0], E')`, a share of every entity `E`, capped at the `E'`
entities that can end, which are the eligible ones and the forced ones. The other entities end by the permutation
`permute/2(K_end, E'', r) < n - f` over the `E''` eligible entities that are not forced, with `f` the forced ones. When `f`
is more than `n`, all forced entities end and no other does. The permutation, and so the bytes, are what they were
when `f` is zero.

**Plan time.** The cost of the bound is per entity. The parents' lifetimes are
read a chunk at a time into arrays, keeping only the two cells of each row, and the layout of a history this process
already holds is found without reading them. Each entity draws inside its own interval, so a `start` histogram has
a table inside almost every entity's interval (a TIMESTAMP chain makes most intervals distinct); the table is not built
bin by bin but read on demand, in whole numbers, by bisection over the bins that meet the interval, with the values the
full table gives for every draw. Plan time therefore grows linearly with the entities and logarithmically with the
bins, and does not grow with the number of distinct parent lifetimes beyond that. The cap-and-pass costs one sort per
round, and a round that finds no entity under its cap ends it. `plan` also notes how many entities were capped, how many versions moved and any
shortfall (the room, point 2); the shortfall is also in the plan document, as point 2 says.

**4. The `activation` column** holds the first start, as section 5 says, so it keeps to the same bound.

**Deterministic everywhere.** The first starts, the rooms, the capped counts and the ended entities are plan-time arrays over the
entities, so a slice, `--jobs`, `--shard`, the column path and the per-row path read the same values; `--scale`
scales the entities and parents as before. The projections (ADR 0027) read the same layout, since a projection's
history is the bound history. `plan` keeps the history's digest apart through the member in `H` and the parent
columns in the driver's document.

**The line.** `fluxsim verify` adds `history:within_parent`, enforced like the other `history:` lines, for an entry
with `parent.lifetime: true`. It covers the entities whose parent row has a start, and counts those that hold: the
first start is not before the parent's start; and, when the parent has ended, no version starts after the parent's
last day, every closed end is at or before it, and an entity whose parent ended by `until` has ended (its last version
is closed, or under `finalVersion` carries `value`). A parent's DATE end is its last day to the last millisecond,
so with a TIMESTAMP `until` the parent ended by `until` when that millisecond is not after it, not when its day is.
For a history with no `end`, a closed last version is allowed only when the parent's last day is at most `until`; an
entity of any other parent keeps its last version open, and one that does not is a violation. `history:one_current`
reads a closed last version as allowed for a history with this key and no `end`.

**When the forced entities are more than the share.** All of them end, and the share the run reaches is higher than the
declared one, by the forced entities beyond it. Nothing is printed: `history:within_parent` and the count of ended
entities in the rows are where it shows.

## Amendment of 2026-11-24: the end share is a share of every entity

Section 5 took `share` of the eligible entities, so a declared share ended fewer entities than it named in a
bundle whose eligible entities were fewer than all of them (an `end` with a `column` needs two versions). A reader
who writes a `share` means that share of the histories, so the share is now a share of every entity:

- `n = min(LRM(E, [share, 1 - share])[0], E')`, with `E` the number of entities and `E'` the number of eligible
  ones, the classes keyed `"0"` and `"1"`. The ended entities are still chosen among the eligible ones, the entity
  of eligible rank `r` ending when `permute/2(K_end, E', r) < n`, so which entities end is the same permutation.
- When fewer entities are eligible than the share asks for, every eligible entity ends, and `plan` says so in a
  note on its text output that names the emit, the declared share and the share yielded, as a decimal. It is advice
  only: no plan document or digest carries it. No bundle is refused for it.
- With `closeLast` and no `column` every entity is eligible, so `E' = E` and the count is the one section 5 gave.
  Every other `end` ends a different set of entities when the eligible ones are fewer than `E`, so **the bytes of
  such a history and of every projection of it move**. A history without an `end` keeps its bytes.
- A run id made before this change replays byte for byte, as every earlier change to generation has kept its
  recorded runs. `plan` writes `historyEndShare: "entities"` into `plan.json` when an emit declares a history with
  an `end` that leaves an entity ineligible (`E' < E`: a `column`, or `finalVersion`). The plan digest covers it,
  and a recorded plan without the key replays `LRM(E', ...)`, the eligible entities alone. Planning the same bundle
  again carries the key, so it moves the plan digest and the run id with the bytes. The kernel stays `0.2.0`,
  which every bundle shares, so a key the plan carries is the narrower marker. Any other value of the key is
  `FLX-2102`.

## Amendment of 2026-11-24: `init` writes the history's parent

Section 13 said `init` writes no `parent`. A foreign key left as a carried column is drawn once, on the entity's
first version, from its own statistics, so the entities were not grouped under a parent and a projection that
copies the parent's key (ADR 0027) rarely agreed with the history. `init` now writes it:

- **The relationship.** The candidates are the history's emit's many-to-one relationships of one column into
  another written expose, that are no key of the emit, whose column no rule, copy or dependency sets and that
  declares no `nullFraction` and is drawn given no column. With one candidate it is the parent, whether or not the
  profile publishes children per parent for it. With several, the parent is the one the profile publishes a
  histogram of children per parent for, when exactly one is; otherwise `init` writes no parent and the notes name
  the candidates, since choosing would be a guess.
- **The children.** A profile counts children per parent in rows, which are versions here, and publishes no count
  of entities. `init` keeps the shape of the published classes as the shape of entities per parent and takes the
  total from the entities (`entities`, not the version rows), so a mean of version rows per parent is unused, with
  that reason. The classes, `minChildren` (1 with the `childless` structural class) and the withheld-class
  assumption are read as for a `counts` entry. A histogram the layout cannot read leaves the parent without
  `children`, and the statistic is unused with the reason.
- **One layout per relationship.** The foreign key declares no draw of its own, and no `counts` entry or spread
  lays the same relationship out: the ledger entries `init` made for it under the counted layout are replaced by
  the parent's.
- **What moves.** A bundle `init` writes for a contract whose versioned expose has such a relationship. A contract
  without one, and every bundle of a profile without a history, keeps its bytes.

## Amendment of 2026-11-24: `init` tracks a foreign key into a reference table

Section 13 carried every candidate column for which the emit declares no levels, which left a foreign key into a
catalogue untracked: a contract's `product_id` has no levels of its own, the keys of the table it points at are
its values. `init` now tracks it:

- **The levels.** A candidate column of the history's emit that is a to-one, one-column foreign key into an expose
  the profile releases whole (`references`) declares that table's keys as its levels, at equal weights, since the
  profile counts no weight for them, and so passes the check that a tracked column declares levels. Its `initial`
  and `transitions` are the history statistic's, as for any categorical column, and their levels must be keys.
- **No guess.** When the table's keys are not all published, or a published change names a value that is no key, the
  column stays carried and its statistics are unused, with that reason. A foreign key into a table that is not
  released whole stays carried as before. A reference table is never the history's `parent`: a catalogue does not
  hold entities.
- **What moves.** A bundle for a contract with such a foreign key in a history whose profile publishes its
  `initial` and `transitions`. Every other bundle keeps its bytes.

## Amendment of 2026-11-25: a dependency beside a history parent

A history with a `parent` lays its entities out over the parent's rows (ADR 0016), so every version of an entity
carries the one parent its entity has. The relationship's foreign key is a function of the natural entity, and a
contract `dependency` that says so is true before it is read. It was `FLX-3403`, since a dependency sets its `to`
columns and the layout sets them too. FLUID's by-entity copy needs exactly this dependency to make a copied column
legal.

- **What is accepted.** A `dependency` on the history's emit whose `to` columns are all foreign-key columns of
  `parent.relationship` and whose `from` columns include the natural entity's column. The layout sets the columns;
  the rule is kept for `verify` and draws nothing, so every emit has the bytes of the same bundle without it. The
  plan lists it as laid out by the parent layout.
- **What stays refused.** A dependency whose `from` lacks the natural entity's column, one whose `to` adds a column
  the layout does not set, and one whose `to` column declares a `nullFraction`, as any dependency on a counted
  relationship (`FLX-3403`).
- **What moves.** Nothing already accepted: a bundle with no such dependency keeps its bytes.

## Amendment of 2026-11-25: a tracked foreign key's levels are keys its parent holds

A tracked column writes the level it is at as the cell and takes no parent row. For a foreign key that makes every
level a value of the parent's key only while the parent's rows hold it, which nothing checked: `--scale` below 1 cuts
a drawn catalogue's rows and its key range and leaves the levels, so a link the bundle held at scale 1 broke at
scale 0.25 with the plan still exiting 0.

- **The check.** `plan` refuses (`FLX-3403`) a tracked column that is the one column of a to-one relationship when a
  level it declares is no key of the parent's column in this plan: for a drawn parent, a value its ordered key
  layout does not take; for a reference expose, a value it does not publish. The message names the expose, the
  column, the first such level and the keys the parent holds (`keys 101 to 107, 7 rows`), and says to plan without
  `--scale` or to stop tracking the column.
- **The floor.** Below scale 1 a drawn parent keeps at least the keys its children's tracked levels name. Its rows
  are floored at the fewest whose key grid holds every level (the parent is scaled as if its rows were that many:
  its key range and its `records` bounds move in proportion), so a link the bundle held at scale 1 holds at 0.25;
  the children keep their own scaled rows. A floor never lowers a count and a parent the scale does not cut is not
  touched. `plan` prints one note naming the parent, the floor and the scale (`note: catalogue keeps 26 rows at
  --scale 0.25, the fewest that hold every level vehicle.part_code tracks`). The floor is worked out for a parent
  whose key is one whole-number column over a declared range with no bins; for another layout the check refuses as
  it does at scale 1.
- **The marker.** A plan that floored some parent writes `scaleFloors: "trackedLevels"` (the plan digest covers it).
  `apply` and `replay` read it back: a recorded plan with it replays floored, and one without it replays with neither
  the check nor the floor, as a plan written before them was made, so its rows keep their bytes. Any other value is
  `FLX-2102`. A fresh plan always runs the check and the floor.
- **What is not refused.** A foreign key that is not tracked: a `declared` pick takes the nearest key the parent
  holds (`KeyGrid.row_near`), so its link holds at any scale. A parent whose key is a several-column key, and a
  relationship of several columns, are not asked. A tracked foreign key into a parent whose key is an identifier
  is asked as the amendment of 2026-11-26 on identifier keys says.
- **What moves.** Nothing for a bundle whose levels are all keys of the parent, at any scale, and nothing for a
  bundle with no tracked foreign key. A bundle whose levels are keys of the parent at scale 1 and not at a scale
  below 1 plans with the parent kept, where it ran before with a link below 100 %. A bundle with a level that is no
  key of the parent at any row count is refused at plan at every scale. A parent whose key is an identifier moves as
  that later amendment says.

## Amendment of 2026-11-25: dates outside the window read the draw

Section 11 refused a date column of a history's emit whose declared range or bins leave `spec.timeBounds`, and read
every edge the column declares. A column read from a noisy profile declares the profile's full year grid as its range
(0001-01-01 to the last instant), because a histogram property's bins must partition the declared range, while its
bins carry weight only inside the window. The grid placed no row outside the window and was refused all the same.

- **The rule.** Section 11 reads the edges the draw can reach. For a column that declares bins, those run from the lower
  edge of its first bin with a positive weight to the upper edge of its last: a weightless bin at either end does not
  count, and one between weighted bins lies inside that hull. For a column with no bins, they are its declared range.
  A column with neither is handled as before.
- **Why.** A range wider than the draw places no date outside the window. The range of a profile's column stays its
  full grid, since the bins must partition it, and the draw is what decides which dates a row can carry.
- **The same edges elsewhere.** The warning of `validate` and `plan` for an emit with no history (the declared range or
  bins leave the run's `timeBounds`) reads these edges too, so the two checks say the same of one column.
- **What stays refused.** A bin outside the window that carries weight, and a column that declares no range and no
  bins unless the history or another source sets it (`FLX-3403`).
- **What moves.** Nothing already planned. This changes what `plan` accepts and not what the generator draws, so a plan
  recorded before replays to its recorded digest, and a bundle accepted before keeps its run digest.

## Amendment of 2026-11-25: `init` writes the open end and the chain a contract declares

FLUID 0.7.7 declares a versioned table's conventions on its validity window's end dimension:
`validityParams.openEnd`, the value an open version's end holds (`null` or a literal of the end
column's type), and `validityParams.chain`, how a closed version's end meets the same entity's next
start (`nextStart`, `dayBefore` or `none`). Section 13 wrote neither, so a history whose end column
the contract requires was never written: an open version had nothing to hold.

- **`openEnd`.** `init` copies a declared literal into the entry's `openEnd`, so an end column the
  contract requires no longer keeps the history out, and each open version ends on exactly that
  value. A required end column with no `openEnd`, or a `null` one, still writes no history, and the
  reason says to declare the value. The value is never read from the profile: a value the data
  holds is not a declaration of what it means.
- **The window.** The declared open end marks no instant, so it gives the run's window nothing, as a
  value in the year 9999 does: a published maximum that is the open end does not stretch the window
  or `until`, and a contract range that ends at it is no range of dates (the 300-year rule of
  `init` does not refuse it). Section 11 does not read an edge of the end column at or past its
  `openEnd`.
- **An end that holds only the open end.** A column whose only published value is the declared open
  end declares no draw: the history writes it on every open version. Without a history to write
  it, `init` refuses the bundle and says why no history is written, since no draw spells such a
  value on every row (`FLX-3125`, whose message now names declaring `openEnd` first).
- **`chain`.** A declared `nextStart` or `dayBefore` sets the entry's chain, whatever share of
  consecutive versions meets so in the profile, and the ledger records that share beside it. A
  declared `none` (versions that need not meet), and `dayBefore` on TIMESTAMP columns, write no
  history, with the reason: the engine generates neither.
- **What does not move.** A contract that declares neither key reads as before, so its bundle is
  unchanged.

## Amendment of 2026-11-25: the open end is one value, and no closed version holds it

Section 5 lets an entry write `openEnd` in the end column of an open version, and `verify` read NULL and that value
alike as open. Nothing kept the two apart: an end column could mix NULL and the value, and a value a closed version
can end on read that version as open. dbt snapshots make the same choice per table (`dbt_valid_to` is NULL, or the
`dbt_valid_to_current` day, such as `9999-12-31`, for current rows; a table changed to it keeps its old NULLs until
they are updated by hand), and SQLMesh's SCD type 2 kinds write NULL only. The value is a day no row ends on.

- **Plan.** An `openEnd` before `until` is `FLX-3403`: a closed version may end there. Under `nextStart` so is an
  `openEnd` inside `timeBounds`, its ends included, since a closed version ends on the next version's start and a
  projection reads an open version up to the window's end, where this value would close it. Under `dayBefore` a
  closed version ends the day before a start, so an `openEnd` at `until` is allowed. A value in the year 9999 or
  later, which a contract's range reads as an open end (`WINDOW_END_LIMIT_MS`, ADR 0024), is after every window.
- **Verify.** A fifth line, `history:open_end`, enforced like the others. With an `openEnd`, an entity holds it when
  none of its versions' ends is NULL and none but its last carries the value: an end column that mixes NULL and the
  value fails, as a closed version that carries it does. Without an `openEnd` the line is present only when some end
  is in the year 9999 or later, an open end the entry does not declare, and an entity with one fails it. The check
  is the one dbt-utils' `mutually_exclusive_ranges` leaves to the author: it fails a NULL upper bound outright, and
  this line says which spelling of the open end the table uses.
- **An `until` in the year 9999.** `until` is the horizon versions start before, not an open end: one in the year
  9999 is `FLX-3403` at plan, pointing at `openEnd` for the open end. A periodic step past such a horizon would
  leave the calendar.
- **What moves.** No row: the generator already writes the value on every open version and NULL nowhere. A run whose
  entry declares `openEnd` gains the line in its report; an entry that a closed version could end on is refused.

## Amendment of 2026-11-26: a forced entity's projections stop at its parent's last day

The amendment of 2026-11-24 keeps every version of a bound history inside its parent's lifetime, and ADR 0027 reads
an open version as valid up to the window's end. Under `finalVersion`, the ending version of an entity forced to
end with its parent (point 3 there) is open, so a periods projection held it for every period up to the window's end,
long after the parent's last day `pe`: the periodic rows left the lifetime the versions keep to.

- **The rule.** In the projections of a history with `parent.lifetime: true`, the open last version of a forced
  entity is valid up to the end of `pe`, the last unit of the parent's lifetime, when that is before the window's
  end. A period it holds under `match` is read against that validity. The written version is unchanged: its end
  cell stays NULL or `openEnd`. Closed versions, and the entities of parents that did not end by `until`, read as
  before.
- **A range on the validity columns** (ADR 0024, the amendment of 2026-11-25) moves no cut. It is folded into where a
  chain must end, so versions start and close inside it, but it closes no version: the open version of a forced
  entity is still valid up to the end of `pe`, even where the range ends before `pe`. The layout keeps `pe` for each
  forced entity apart from the chain's end for this reason.
- **Events and snapshots** read no validity beyond a version's start, so they are unchanged.
- **What moves.** The periodic rows of a bundle that declares `parent.lifetime: true` with a `finalVersion` end and
  an entity forced to end, and that emit's count, so its plan digest and run id. No released version carries the key.
  Every other bundle, and every recorded plan, keeps its bytes. `--jobs`, `--shard` and the column and per-row paths
  read one layout, so they agree.

## Amendment of 2026-11-26: `init` keeps a parent whose key a held dependency sets

The amendment of 2026-11-24 that lets `init` write the parent left out every relationship whose column a rule, copy
or dependency sets. The amendment of 2026-11-25 then let the plan accept a dependency the parent layout holds, but
`init` still left the parent out for it. The history was written with no parent, so no expose keyed by the natural
entity could be its snapshot. Such an expose was drawn apart from the history, with keys of its own, and most of the
versions named an entity that it did not hold.

- **What is accepted.** A candidate whose column only contract dependencies set, each from columns that include the
  natural entity's column and to that column alone: the dependency the plan accepts beside a parent. `init` writes
  the parent as for any candidate, and a snapshot or periods entry that needs it can follow.
- **What stays refused.** A column that a derive or a copy sets, and a dependency whose `from` lacks the natural
  entity's column or whose `to` names another column. A `nullFraction` on the column refuses the parent as before.
- **What moves.** A bundle `init` writes for a contract with such a dependency. Every other bundle keeps its bytes,
  and no plan changes: the generator and replay of recorded plans are untouched.

## Amendment of 2026-11-26: a rule that reads `previous` runs on a history that keeps to its parent's lifetime

Section 7 lets a rule read the version before a row. The pass that evaluates such a rule laid out the entity's
versions without knowing its own plan, and a history that keeps to its parent's lifetime lays its versions out from
the parents' lifetimes, which are read through the plan's own `parent` relationship, so the plan was refused
(`FLX-3403`, "whose plan this pass does not read"). The pass now knows its plan, and the layout is the one every other
pass reads.

- **What moves.** A bundle with both a `parent.lifetime` and a rule that reads `previous` now plans. Every other plan
  keeps its bytes, and no recorded plan changes: no plan was made for that shape.

## Amendment of 2026-11-26: `init` writes the activation and the end a contract's lifecycle declares

FLUID 0.7.7 declares on the natural entity of a model with a validity window how its versions begin and
end, as `lifecycle`: `activation`, a column of the expose and of the window's type that holds the start of the
entity's first version on every version; `endMarker`, a column and a value, the value an ending version holds
and no earlier version does (dbt's `hard_deletes: new_record`); and `currentFlag`, a column and a value that one
version of each entity holds, its last. Section 13 wrote neither `activation` nor a `finalVersion`, since a profile
says which column holds an entity's first start no more than which value marks an end. The contract does.

- **`activation`.** `init` writes the declared column as the entry's `activation` when the expose has it at the
  window's type and it is no key, no validity column and no column a rule, a dependency or a copy sets. Every version
  of an entity then holds its first start (section 5). Otherwise none is written, and the assumptions say which
  of these it fails.
- **`endMarker`.** When the column declares the value as a level, besides one with a weight, and is no key, no
  validity column, no column a rule sets, declares no `nullFraction`, is drawn given no column and no column is
  drawn given it, `init` writes `end: {mode: finalVersion, share, column, value}`. `share` is the closed share of
  last versions that `closeLast` took, a share of every entity (the amendment of 2026-11-24). Where the profile
  tracks the column, its first values and moves are written without the value, which only the ending version holds
  (section 5); a tracked column with no published first value is carried instead, and a carried column's weight for
  the value becomes 0. The change into the value is no change reason, so the profile's `reasons` share of that
  column, which counted it, is written as published. When no last version is closed, or the contract's column or
  value does not fit, the end is what section 13 wrote, `closeLast` or none, with the reason.
- **`currentFlag`.** The engine has no construct that sets a value on an entity's last version alone: a window over
  siblings (EXPR-SPEC-2 section 5) partitions by a to-one relationship, and an entity's natural key is never a
  relationship's column on a history's emit (section 3), so no window spans exactly an entity's versions; `previous`
  reads backwards only; and `history:one_current` checks an open end, not a column. `init` writes nothing for it, the column keeps its own draw, and the assumptions say so.
- **What does not move.** A contract with no `lifecycle` gives the bundle it gave before. Each member the
  entry does not use is named in the assumptions with its reason, as ADR 0014 requires.

## Amendment of 2026-11-26: `init` tracks a whole-number foreign key published as a category

Section 13 tracked a column only when the emit declared levels for it, or when it was a foreign key into a table
the profile releases whole, whose keys became its levels. A whole-number foreign key into a table that is not
released whole declared none, because its category was read only for text columns, so the history carried it.

- **Levels.** A foreign key (one column, to-one) that the history lists, that no rule, dependency or copy sets and
  no clique draws, whose profile publishes at most 64 whole-number levels (`levelHistogram/1` or `support/1`), and
  that references a table not released whole, declares those levels with their published weights, as any column
  drawn over few integer levels does. The history then tracks it by section 5 like any categorical column.
- **The keys.** The referenced key must hold every level. It is laid out as the span of the levels of all the
  columns that reference it (the rule that widens an assumed key to its foreign keys), which is one value per row
  when the levels lie inside as many consecutive values as the table has rows. When they span more, the end levels
  are dropped one at a time, the one with the smaller published share first and the higher value on a tie, until
  they fit, provided the dropped levels hold at most 5 % of the rows together; the others take their shares
  renormalised, the history's first values and moves are written without the dropped levels, the column's
  `levelHistogram` property is not written (the profile's histogram counts the dropped levels), and the assumptions
  name the dropped levels and their share. Above 5 %, when the key's own min and max are published, or when the key is a foreign key itself, no level
  is declared, the column is carried as before, and the ledger says what to change.
- **What does not move.** A column that is no foreign key, a foreign key whose published levels are not whole
  numbers, and a table released whole (section 13's own route) give the bundle they gave before.

## Amendment of 2026-11-26: a `within` entry that keeps the start inside the parent's life is `parent.lifetime`

The amendment of 2026-11-24 keeps a history's versions inside the life of its parent with
`history[].parent.lifetime: true`. The same bound written the general way, a `within` entry on the
history's emit for the parent relationship and the history's validity start, was refused as two sources
for the start (`FLX-6203`), though it asks for nothing else.

- **Read as the key.** A `within` entry whose emit is the history's, whose relationship is exactly the
  history's `parent.relationship` (one hop), whose `date` is the history's validity start and that carries
  no `offset`, `target`, `exposure` or `lifetime`, is read as `parent.lifetime: true`. It is applied once,
  through the parent's lifetime, and is no `within` entry afterwards, so nothing else sets the start from
  it. The reading happens where the bundle is resolved, before any reader of the entries, so the bundle
  draws the rows the same bundle written with the key draws. A target emit with no `lifetimes` entry is
  `FLX-3403`, as for the key.
- **What stays refused.** Every other `within` entry on a column the history sets (another relationship,
  a path of several hops, the validity end, the activation column, an entry that draws its own offset or
  target) is `FLX-6203`. The message for a history with no parent also names `parent.lifetime`, with the
  parent relationship it needs.
- **What does not move.** A `within` entry on a date the history does not set, and a bundle that declares no
  such entry, read and hash as before.

## Amendment of 2026-11-26: an undeclared open end gives the window nothing

The amendment of 2026-11-25 about the declared open end keeps the value out of the run's window: it marks no
instant. A validity end that publishes values in the year 9999 with no `openEnd` declared for it left the window
stretched to the year 9999, since only a column whose every value lay in that year gave the window nothing.

- **Not read as a declaration.** `init` still never reads an open end from a profile: a value the data holds is not a
  declaration of what it means, so no `openEnd` is written for it. A history whose end would draw such a value is
  still not written: the plan refuses a bin or range that leaves `spec.timeBounds` (`FLX-3403`), where it refused an
  `until` in the year 9999 before.
- **The window.** The end column of a validity window whose contract declares no `openEnd` has its span cut at the
  year 9999, as a declared `openEnd` cuts it: the window keeps to the dates the data holds, the way a contract range
  that ends in the year 9999 gives it nothing. The `time window` note names the column and says to declare the
  `openEnd` on it. Another date column that reaches the year 9999 is not an end and keeps its span, and the
  long-window note names it. An `openEnd` declared as `null` is a declaration, so such an end keeps its span.
- **What does not move.** A bundle with no such column, and one that declares the `openEnd`, read and hash as before.

## Amendment of 2026-11-26: a tracked foreign key into a parent keyed by an identifier

Section 6 asks a tracked foreign key's levels of the parent's keys, and the amendment of 2026-11-25 left a
parent whose key is an identifier out. An identifier key is a keyed permutation of the row index over every
identifier of its `identifierLength` hex characters. A parent with fewer rows than there are identifiers holds
a few of them, scattered by the key, so no level a contract declares is one of its keys: a tracked foreign key
writes its level as the cell, and every cell of the child then named a key the parent lacked (the link scored
far below 100 % at `verify`).

- **The rule.** A tracked foreign key into an identifier parent is refused (`FLX-3403`) unless the parent holds
  every identifier of its length: its rows in this plan equal `16 ** identifierLength`, and it is not a UUID.
  Then it holds every lower case hex string of that length, and each level is a key when it is exactly that many
  characters of `0-9a-f`. A level of another form (not hex, upper case, shorter or longer) is refused, and the
  message names it.
- **Rows in the plan.** The count is the parent's rows at the plan's `--scale`, so a parent that fills the space
  at scale 1 and is cut below 1 holds a part of it and is refused, as a sparse parent is. The floor of the
  amendment of 2026-11-25 raises only a parent whose key is one whole-number column, and does not raise this one.
- **What is not asked.** A partial space is not tested level by level: whether a given identifier is among the
  first `rows` permuted values needs the inverse of the permutation, which the key does not carry, and a loop
  over the space is unbounded. A parent that holds fewer identifiers than its length allows is refused for every
  level, which is the answer a plan can give without drawing the parent.
- **What moves.** A bundle with a tracked foreign key into a dense identifier parent, which a plan refused
  before, plans and verifies at 100 %. A bundle whose identifier parent is sparse, or cut by `--scale`, is
  refused as it was. No row changes for a plan accepted before: a plan
  with such a link was either refused or accepted with a broken link, and a broken one is refused now. A foreign
  key that is not tracked takes a key the parent holds, as before.

## Amendment of 2026-11-26: a history whose lengths a release withholds

A release may publish a versioned table's start and end as a `history` entry and withhold that entry's
`versionLength`, and with it `versionsPerEntity`, `firstStart`, `chain` and `lastVersion`. Section 13 wrote no entry
then, the table's date pairs were all that were left, and the end was drawn from its own histogram, so a version could
end before it started.

- **A history that can be written.** `init` writes it from what the release does publish, and each member it supplies
  is an assumption in the register. The entities are the natural entity's published distinct count, with the
  versions spread over them as evenly as whole counts allow. The `start` is the first starts' histogram when the
  release publishes it, and else the start column's own histogram, which is exact where each entity has one version.
  The chain is the contract's, else `nextStart`. The `length` is the histogram of gaps that turns the starts into the
  closed part of the end's histogram, found by the Richardson-Lucy update from an even start (`author/gap_fit.py`):
  each bin of starts and of gaps spread evenly, a fixed count of updates, whole-number arithmetic so no machine
  reads a different byte. Where the starts or the end are drawn from no histogram, the length is even over the run's
  window. The `end` share is the share of entities with no open version in the end's histogram, read at the
  horizon; with the end drawn from no histogram, no history is written for the length and the table's start and end are one
  lifetime (below). With no closed part in that histogram, the share of the lifecycle's end marker stands in, and
  with neither no entity ends.
- **What is not written.** A history is not written from withheld lengths where the release publishes a column on
  the start's day that no `lifecycle.activation` keeps with the first start: the history would draw the two apart.
  Nor where a required end has no declared `openEnd` (section 13's amendment of 2026-11-25). The table's start and
  end are then one lifetime (ADR 0024), with the same gaps, and a published date pair for the two is still that
  pair's.
- **The check it makes.** Independent draws turn the starts into the ends only as far as the ends' cumulative
  share stays at or below the starts' (Strassen); `init` reports the largest excess, and the total variation between
  the ends the gaps give and the published ones. An end column's histogram property that the gaps miss by more than
  its tolerance is left out, with a note.
- **What does not move.** A release that publishes its lengths, and one that publishes the pair as a date pair, keep
  every byte. No generator or plan code changes.
