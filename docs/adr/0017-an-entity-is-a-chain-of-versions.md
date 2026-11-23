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
value, `FLX-6203`.

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
