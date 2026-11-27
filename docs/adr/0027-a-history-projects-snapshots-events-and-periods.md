# ADR 0027: a history projects a snapshot, change events and periodic rows: row counts derived at plan time, Debezium's `c`, `u` and `d`, periods laid from the window's start, and a longer window that appends periods

## Status

Accepted 2026-11-22. It projects the histories of [ADR 0017](./0017-an-entity-is-a-chain-of-versions.md),
allots rows exactly as [ADR 0016](./0016-children-per-parent-are-counted.md) section 6 allots levels, reads a
version's final values as [ADR 0022](./0022-rows-read-their-linked-rows.md) section 3 reads a parent's, and draws
a lag inside an interval as EXPR-SPEC-2 section 6 draws a value there. Its module facts live under [ADR 0018](./0018-module-facts-live-in-the-flux-bundle.md)'s namespace.
Everything below generates under kernel `0.2.0` only.

## Context

A history (ADR 0017) is one table: every version of every entity. The tables a warehouse derives from such a
table are other exposes of the same contract:

- **a snapshot**: one row per entity holding its current state, the current table beside a dimension's
  history, or the rows of a dbt snapshot whose `dbt_valid_to` is NULL;
- **change events**: one row per change, as a change data capture stream writes them, each with the state
  before and after;
- **periodic rows**: one row per entity and period while a version is valid, a periodic snapshot fact such as
  a subscription's monthly rows, sometimes several kinds of row per period, such as one usage row per service.

On the engine today each would be drawn on its own statistics: a snapshot row would not equal its entity's last
version, an event would not match a change, and a monthly row could fall in a month in which its version was not
valid. Each would also need a declared `rows`, although its number of rows follows from the history: `plan`
digests the declared `rows` of every emit, so a projected emit's count has to be worked out at plan time and
recorded in its place.

Borrow-before-build search, 2026-11-22. The codebase came first: ADR 0016 section 6 already allots a column's
levels exactly over a row domain, ADR 0024 section 5 already draws a value from a histogram inside an interval
with a uniform fallback, EXPR-SPEC-2 section 6 draws a value from a histogram inside an interval, and ADR 0017
gives every version's validity and values from its entity alone.
Searches: "Debezium change event envelope op c u d r before after source ts_ms", "derive change data capture
events from SCD type 2 history table before after op periodic snapshot fact from validity intervals", "dbt_utils
date_spine datepart start_date end_date excluded periodic snapshot fact table one row per period". Fetched the
Debezium PostgreSQL connector reference (documentation `stable`, 3.7) and dbt-utils' `macros/sql/date_spine.sql`
and README at the tag `1.4.1`. Versions and licences were read from GitHub and PyPI on that day.

| Name | What it does here | Licence | Taken |
|---|---|---|---|
| Debezium 3.7.1.Final (the latest final release; 3.8.0.Alpha2 is a pre-release), change event values | `op` is `c` create, `u` update, `d` delete, `r` read (snapshots only), `t` truncate, `m` message; `before` is NULL for a create, `after` NULL for a delete; `ts_ms` is when the connector processed the event, `source.ts_ms` when the change was made | Apache-2.0 | the event vocabulary: `c`, `u` and `d` with before and after columns, `at` as `source.ts_ms` and `date` as `ts_ms`, a lag between them; `r`, `t` and `m` are not generated |
| dbt-utils 1.4.1, `date_spine` | periods from `start_date` by a `datepart`, each `dateadd(datepart, n, start_date)`; the start is included when aligned, the end excluded | Apache-2.0 | the periods: from the window's start by the grain, the window's end excluded |
| dbt-core 1.12.7 snapshots | the current rows are those whose `dbt_valid_to` is NULL; `hard_deletes: invalidate` leaves a deleted key without one | Apache-2.0 | a snapshot is each entity's last version, and `ended: drop` is a current table without the ended entities |
| dbt Discourse, "Finding active days for a subscription" (date spining) | a spine joined to validity on `start <= date` and an end that allows NULL on current rows | not stated | confirms the period join: a version holds a period it overlaps, an open end holds every later period |
| AutomateDV, as-of date tables | a spine of as-of dates over which point-in-time tables are built | Apache-2.0 | pattern: `match: end` and `match: start` read one version per entity and period |
| Delta Lake change data feed (delta-spark 4.4.1) | `_change_type` `insert`, `update_preimage`, `update_postimage`, `delete`, one row per image | Apache-2.0 | pattern only: before and after are two images of one change; here one row carries both |
| Databricks AUTO CDC FROM SNAPSHOT (docs) | a change feed inferred from consecutive snapshots | proprietary documentation | the reverse direction; none |

No tool found projects a generated history into its snapshot, its change events and its periodic rows with row
counts known before a row is written, or keeps a period's rows when the window grows. No new dependency is
needed.

## Decision

### 1. Kernel 0.2.0 only, and only where a bundle asks

The namespace gains three keys the engine did not read before: `snapshots`, `events` and `periods`. A bundle
that declares none of them generates, plans and digests exactly as before under both kernels. Under kernel
`0.1.0` each is refused with `FLX-3105`, naming the kernel. Each entry changes the rows of the emit it names, so
it is bound into that emit's entry for `H`, with a member `projects` that holds the history's emit and that
emit's own entry for `H`, which holds its `history` entry, so a changed history moves its projections. No plan
document enters an entry for `H`: a plan document holds the window, and the seed tree's root folds in every
`H`, so a window that grows would move every stream. `G` leaves the keys out.

### 2. A projected emit, and its rows derived at plan time

An emit that a `snapshots`, `events` or `periods` entry names is projected: its rows follow from the history
of its entry's `of`, which names an emit that a `history` entry declares.

- It declares no `rows`, and a declared one is `FLX-3403`, naming the entry. At plan time its number of rows is
  worked out from the plan alone (the history's layouts, chains, ended entities and change reasons, and for
  periods the tracked values and each period's allotments, none of which needs a cell), and the plan document
  records it as the emit's `rows`, with `rowsFrom: {of, projection}` saying where it comes from. The recorded
  count enters `planDigest` only, never an entry for `H`.
- `--scale F` scales the history's `entities` and `rows`, and a projection's count follows from the scaled
  history; it is not scaled again. `--shard I/N` takes contiguous shares of the derived rows, as it does of
  declared ones.
- Every process that writes a slice of a projected emit works out what the slice needs from the history once:
  the layouts and the chains, a cost that follows the entities, or for periods the versions.
- At most one entry names an emit. A projected emit with a `history` of its own, a `reference` block, or a
  `counts` entry over one of its relationships is `FLX-3403`. Another emit may pick its parents in a snapshot
  or an events emit, whose row count is known at plan time and does not change with the window. A relationship
  whose parent is a periodic emit is `FLX-3403` in this version, since that parent's rows grow with the window
  and every pick over them would move.


### 3. Snapshots

The namespace key `snapshots` lists `{emit, of, ended}` entries.

- One row per entity of the history, in entity order, so grouped by parent as the history is.
- Each column of the snapshot's contract is a column of the history's emit, of the same name and type, else
  `FLX-3403`. It holds the entity's last version's final value, its rules included.
- `ended: keep`, the default, keeps every entity, an ended one with its last version. `ended: drop` leaves out
  the entities the history ended (ADR 0017 section 5), as a current table of live entities does, so the rows are
  `E` less the ended count, known at plan time.
- It draws nothing and adds no path. Its key is the natural entity, unique per entity.

### 4. Change events

The namespace key `events` lists `{emit, of, date, at, lag, on, op, type, before, after, when}` entries.

- **One event per change.** In entity order, each entity has a `c` event for its first version, a `u` event for
  each later version, and a `d` event when the history ended it: under `finalVersion` the change into its final
  version is the `d`, and under `closeLast` the close of its last version is. `on`, a list of `c`, `u` and `d`
  (all three by default), keeps the listed ones.
- **`at` and `date`.** `at`, optional, names a DATE or TIMESTAMP column that holds the change's instant: the
  version's start, or for a `closeLast` `d` the last version's end, as Debezium's `source.ts_ms` holds the change
  in the source. `date`, required, names a column of the same type that holds when the event was recorded, as
  `ts_ms` does: `at` plus a lag.
- **The lag.** `lag`, optional, is a histogram whose edges are whole days, written as EXPR-SPEC-2 section 6
  writes a `distribution` (`bins`, `binWeights`, `closed`). It is drawn with one `uniform53` at position 0 of
  `("sim", sim_id, "gen", exposeId, "lag", entity, version)`, at the date column's unit, inside the interval
  from zero to `until` less the change's instant, as EXPR-SPEC-2 section 6 draws a value inside an interval,
  and uniformly over that interval with one `bounded` at the same position when no bin meets it. The `d` of a `closeLast` entity takes the version ordinal one past its
  last version. Without `lag` the event is recorded at its change. So no event is dated after the history's
  `until`.
- **Before and after.** `before` and `after` map an event column to a column of the history's emit. Under
  `before` it holds the previous version's value, NULL on a `c`, and on the `d` of a `closeLast` entity the last
  version's own value, the state that was deleted; under `after` this version's value, NULL on a `d`. They are
  Debezium's `before` and `after` structures, one column per field. A `before` column the event contract
  requires is `FLX-3403` when `on` keeps `c`, and an `after` column it requires is `FLX-3403` when `on` keeps
  `d`, since those events write NULL there.
- **`op` and `type`.** `op`, optional, names a text column that holds `c`, `u` or `d`. `type`, optional,
  `{column, values}`: `values` maps `c`, `d`, `end` and each tracked column to the text written in `column`. A
  `u` takes its change reason's text (ADR 0017 section 6), and the `u` of an ending version under `closeLast`
  with an `end` column (ADR 0017 section 5) takes `end`'s, so an event's type follows its reason. Every kept
  op, every reason, and `end` when the history has ending versions, need a text, else `FLX-3403`.
- **`when`**, optional, a FLUID SQL/1 predicate over the version the event is about (for a `d`, the entity's last
  version): its tracked columns, its validity, its activation, and through `previous.` those of the version
  before. An event exists only where it is TRUE. It reads nothing a cell draws, so the count is known at plan
  time, and a predicate that reads any other column is `FLX-3403`.
- **Other columns.** An event column that names a column of the history's emit and no mapping holds the value of
  the version the event is about: the new version for a `c` or a `u`, the last version for a `d`, such as the
  entity's key. Every other column is drawn from the event emit's own statistics on the generator cell path at
  its row.

### 5. Periodic rows

The namespace key `periods` lists `{emit, of, column, grain, match, kinds}` entries.

- **The periods.** Period `p` runs from `timeBounds.start`'s day plus `p` grains to the day `p + 1` grains on,
  for `p` from 0 while it starts before `timeBounds.end`: as dbt-utils' `date_spine` lays its periods from its
  start, the window's end excluded. `grain` is `day`, `week`, `month`, `quarter` or `year`, and defaults to the
  period column's FLUID `timeGranularity` when it declares one (`FLX-3403` when neither gives one). Each period's
  first day is counted from the window's start, never from the period before, and a month added to a day the
  month lacks lands on the month's last day, as flux-expr/3's `ADD_MONTHS` does (ADR 0013). `column` names
  a DATE column of the emit that holds the period's first day, which lies inside the window.
- **The pairs.** `match` says which versions a period holds: `overlap`, the default, every version valid at some
  instant of the period, so a period with a change holds both versions and each version's rows cover exactly
  its periods; `end`, the version valid at the period's last instant inside the window, one per entity at most,
  a periodic snapshot at the period's close; `start`, the version valid at its first instant. An open version is
  valid up to the window's end, and an entity holds no period before its first start or after a closed end. A
  held (version, period) is a pair.
- **Kinds.** `kinds` lists `{count, when, overrides}`; without it there is one kind, one row per pair.
  - `count` is a whole number of rows per pair, or `levels` with `levelWeights` over whole counts. Then in each
    period the `M` pairs the kind holds take `LRM(M, weights)` per level, keyed as ADR 0016 section 6 keys
    classes, and the pair of rank `r` among them, in row order, takes the level of slot `permute/2(K, M, r)`
    laid out level by level, where `K` is the key of `("sim", sim_id, "gen", exposeId, "kind", k, p)` with `k`
    and `p` the kind's and the period's positions written in decimal. So every period meets the kind's shares
    exactly, up to the largest remainder, and no period's allotment reads another period.
  - `when`, optional, a FLUID SQL/1 predicate over the pair's version (its tracked columns, its validity, its
    activation) and the period column: a pair the kind does not hold has none of its rows. As for events, it
    reads nothing a cell draws (`FLX-3403` otherwise).
  - `overrides`, optional, maps a column to a text written as the column's type on the kind's rows, such as the
    kind's own label.
- **Columns.** The period column; the kind's overrides; each column that names a column of the history's emit
  holds the pair's version's final value, so a periodic row carries the version valid in its period; every
  other column is drawn from the periodic emit's own statistics on the generator cell path at its row.
- **Its key.** A periodic emit's primary key, a whole-number column, is laid out from the low end of its declared
  range, one step per row: row `i` takes `lo + i`, never a point spread over the row count, so a longer window
  appends keys and keeps every earlier one, and the check that the range holds the rows stays. Another type of
  primary key, and a column under `allocation: exact`, which reads the row count, are `FLX-3403` on a periodic
  emit in this version.
- **Order.** Period by period, then entity, then version, then kind, then the kind's rows.
- **The count** is the sum over periods, kinds and held pairs of the rows each takes. `plan` works it out in
  one pass over the versions, each giving its first and last period, and the allotments of each period, a cost
  that follows the versions and the periods. A process that writes a slice finds its first period by bisection
  over the periods' counts and walks the entities of the periods it covers.

### 6. Paths, positions and vectors

| path | kind | holds |
|---|---|---|
| `("sim", sim_id, "gen", exposeId, "lag", entity, version)` | stream, two ordinals | position 0: an event's lag |
| `("sim", sim_id, "gen", exposeId, "kind", k, p)` | permutation key, names only | one kind's rows per pair in one period |

**Additive** under KERNEL-SPEC-2 section 12: `lag` and `kind` are literals no other shape has at the place after
`exposeId`, beside ADR 0017's, so their addresses are new; they are read only for a bundle that declares
`events` or `periods`; nothing that exists moves; and `tests/vectors/kernel2-history-vectors.json` pins their
keys, counters and blocks, with the derived counts and allotments of chosen entries. A snapshot draws nothing.
A projected emit's own cells draw on the generator cell path at its rows, as any emit's do; an emit becomes
projected only by an entry that names it, which is what changes its rows.

### 7. A window that grows appends periods

Extending `spec.timeBounds.end` past a history's `until` keeps every projected row made before:

- a snapshot and the change events read the history, which a longer window does not move (ADR 0017 section 10),
  and every event is dated at or before `until`, so both are the same, byte for byte;
- periodic rows are laid from the window's start, so the periods before the old end keep their place; no
  version starts or ends after `until`, so each of those periods holds the same pairs, a period that the old end
  cut in two included; each period's allotment is keyed by the period and reads no other period; and the order
  puts every row of an earlier period before every row of a later one, so the earlier rows keep their indices,
  their keys, their cells and their bytes. The periods after the old end are new rows, appended after them.

A test runs one bundle under two windows with the same `until`: the snapshot and the events are equal byte for
byte, the longer window's periodic rows begin with the shorter window's rows byte for byte, and the digests of
every emit except the periodic one are equal.

### 8. Format changes

- **The plan document**: a projected emit's entry records its derived `rows` and `rowsFrom`, and its entry for
  `H` holds `projects`. Both appear only for an emit an entry names, so every other plan document is unchanged.
- **No profile or request change.** A profile that publishes lags, rows per entity, period and kind, and the
  share of valid periods with rows is a later format's, so `fluxsim init` writes projections from the contract
  alone, with no lag and one row per pair.
- **No scorecard change**: the checks of section 10 are `rules` lines.

### 9. `fluxsim init`

For each versioned expose that `init` gives a history (ADR 0017 section 13), it reads the contract:

- **A snapshot**: an expose without a validity window whose primary entity's columns are the history's natural
  entity, and whose every column the history's expose declares with the same name and type, gets a `snapshots`
  entry with `ended: keep`.
- **Periodic rows**: an expose without a validity window, with a to-one relationship whose `toColumns` are the
  natural entity, exactly one DATE dimension that declares a `timeGranularity`, and no column whose levels lie
  within `c`, `u` and `d`, gets a `periods` entry on that dimension and its grain, with `match: overlap` and one
  kind. A FLUID entity names one column, so the rule reads the dimension, not a key of two columns.
- **Change events**: an expose without a validity window, with such a relationship, a column whose declared
  levels lie within `c`, `u` and `d`, and a `defaultAggTimeDimension`, gets an `events` entry with that column as
  `op`, the dimension as `date`, and for each column `<c>` of the history's expose, the columns named
  `before_<c>` and `after_<c>` as its `before` and `after`.

An expose that meets part of a rule is recorded as unused, with the part it misses, as ADR 0014 requires.

### 10. Checks

`fluxsim verify` checks each projection on the run's rows. Each line is enforced, so one violating row fails the
run:

- `snapshot:equals_last_version`: each snapshot row equals, column for column, its entity's last version, the
  one whose start is latest; each entity appears once, and under `ended: drop` exactly the ended ones are absent.
- `event:matches_transition`: per entity, the events equal its changes as a set, independent of row order: one
  `c` for its first version, one `u` per later version, one `d` when it ended, each with `before` and `after`
  as section 4 defines them, and `date` not before `at`.
- `event:transition_matches`: each `u` other than an ending change moves exactly one tracked column, along a
  transition the history's `transitions` give a weight above zero. An ending change moves its `end` column to
  `value` (ADR 0017 section 5) and is checked for that alone.
- `period:within_validity`: each periodic row's period holds a version of its entity under the entry's `match`.
- `period:matches_valid_version`: each periodic row's columns that name the history's columns equal a version of
  its entity that the period holds.

A check that needs the history's rows, when the history's emit is hidden (ADR 0024 section 3), is listed as not
scored, with that reason.

## Consequences

- A bundle that declares no projection keeps every byte and every digest under both kernels. The golden of
  ADR 0017 covers the history; a second golden pins a contract with a snapshot, an events and a periodic expose
  declared and no projection keys, with origin/main's digests.
- A projected emit's row count is the history's to say, so a bundle can no longer give it a number that
  disagrees: the snapshot has one row per entity, the events one per change, the periods one per held pair and
  kind.
- Tests cover kernel `0.2.0`'s column path against the per-row path for each projection, `--jobs 1` against
  `--jobs 4`, `--scale` below 1, down to one version per entity, and `--shard I/N` with shares that cut
  across period boundaries.
- Periodic rows are written period by period, and a consumer that needs them grouped by entity sorts them. The
  snapshot and the events follow the history's order, parent, entity, version.
- A history may be hidden (ADR 0024 section 3) and still projected: its rows are generated, read by its
  projections and never written, which is how a source that publishes only its current table and its change
  stream is imitated.

## What would change this decision

- A profile that publishes event lags, rows per entity, period and kind, and the share of valid periods with
  rows, which would let `init` write them.
- A condition on an event or a kind that reads a drawn column, which needs cells at plan time or a count that is
  not known before the rows.
- Periods aligned to the calendar rather than to the window's start, TIMESTAMP period columns, and grains below
  a day.
- An event stream in date order rather than in entity order, which needs a sort over all events.
- Debezium's `r`, a snapshot read at the start of a stream, if a source publishes its stream from a snapshot.
- A history that grows past `until` (ADR 0017's "What would change this decision"), after which events would
  append too, and periods would hold new versions.

## Amendment of 2026-11-24: a projected row's rules run after its projected cells

Sections 4 and 5 say which cells an events or a periodic row takes from the history and which it draws, and said
nothing of when the emit's own rules run. The generator ran them over the drawn cells and set the projected cells
afterwards, so a derive that read the period column, a column of the version, a kind's override, `op`, `at`, `date`
or an image read a drawn value that the row no longer held, and `fluxsim verify` failed the run on its own rule
lines. This amendment fixes the order:

- **The order.** An events or a periodic row is its emit's drawn cells, then the cells the entry sets (section 4's
  `op`, `at`, `date`, `type`, images and columns of the version; section 5's period column, columns of the version
  and the kind's overrides), then the emit's rules: a contract's derives and checks, the FLUX-side rules, the
  copies and the links, in the order the plan sets their columns. A rule reads a cell the entry set as it reads any
  other cell of its own row. An emit whose rules hold a window over siblings is made whole first, as any such emit is.
- **One source per column.** A rule that sets a column the entry sets is `FLX-6203` at plan, naming the column, the
  entry and the rule: the entry is the column's source. A derive on a foreign key stays `FLX-6210`, as on any emit.
- **A path through a relationship is refused when the entry sets its key.** A rule that reads through a
  relationship (a derive over `relationship.column`, a copy, a path under an as-of link) or a conditional link
  reads the row's drawn pick of that relationship, not the value in the foreign-key cell. When the entry sets the
  relationship's foreign-key column (a column of the version the row carries), the cell and the pick name different
  rows, so such a rule would read another row's value. It is `FLX-3403` at plan in this version, naming the rule,
  the relationship and the column; a rule over the row's own cells is not affected.
- **A `given` draw over a set cell is refused.** A column drawn `given` another is drawn from the level the
  other column's own draw made in the row, which the entry then replaces. A column of an events or periodic emit
  drawn `given` a column the entry sets (`model` on a month, an image on a change) follows a level the row does
  not hold, so it is `FLX-3403` at plan, naming both columns and the entry. A column the entry sets itself may
  declare a `given`, which its cell then replaces.
- **What moves.** The rows of an events or a periodic emit whose rules read a cell the entry sets, and so their
  digests. Every other emit, and every such emit without rules, keeps its bytes. A snapshot is unchanged: its rows
  are the history's, rules included.

## Amendment of 2026-11-24: a `when` reads the history's end column

Sections 4 and 5 let an events `when` and a periods kind's `when` read the version's tracked columns, validity,
activation and `previous.`, and no other column. A history's end column (`history[].end.column`, such as `status`)
was one of the others, so a kind could not leave out the ending versions by name. It is readable now, and only a
`when` that names it changes:

- **The value.** On an ending version the column holds `end.value`. On any other version its cell is drawn, from
  the levels its statistics give a weight above zero; the end value is never one of them, which the history already
  refuses (ADR 0017 section 5). A column that the history tracks holds the tracked level and needs no more.
- **At plan time, with no cell.** The `when` is evaluated over each level the column can draw on a version, with the
  version before it, through `previous.`, holding the same level (a carried column keeps the entity's first value).
  It is known at plan time where every level gives one result, as `status <> 'retired'` does: false on an
  ending version, true on every other. Where the results differ for some version, the `when` is `FLX-3403`, naming
  the kind (or the events entry) and the column and saying that the result depends on a drawn value.
- **One source.** A rule that sets the end column (a `derive`, a dependency's `to`, a FLUX-side rule or a copy) is
  `FLX-6203` at plan, naming the column and the rule, whatever its `when`: the history writes `end.value` there and
  a drawn level elsewhere, and `history:ended_is_last` holds the rows to it, so the rule would be a second source and
  a `when` would read the draw, not the rule. A column the history does not carry (and does not track) is `FLX-3403`
  where a `when` reads it.
- **Opt-in.** A `when` that does not name the end column is as section 4 and 5 say, and keeps its bytes.

## Amendment of 2026-11-24: from and through on a periods entry

Section 5 lays a periods entry's periods from `timeBounds.start`'s day while they start before `timeBounds.end`, so
an entry could not hold fewer periods than its window. Two optional keys, kernel 0.2.0 only, both DATE texts
`YYYY-MM-DD`, bound the range:

- **`from`.** The first period starts on this day: the anchor every period is counted from, as `timeBounds.start`'s
  day is without it. Refused (`FLX-3403`, naming the entry and the key) before `timeBounds.start`'s day or not
  before `timeBounds.end`.
- **`through`.** The last period is the last one that starts on or before this day. It is inclusive on purpose, as
  the word reads; the window's end stays excluded. Without it, every period that starts before `timeBounds.end`.
  Refused before `from` (or before `timeBounds.start`'s day without one) or not before `timeBounds.end`.
- **Ends.** A period's end is the next period's start, or the window's end for the last period of the window,
  unchanged: a `through` inside a period leaves that period whole.
- **What follows.** A version holds no period outside the range, so the count, the layout, the slices and the kinds
  follow, and the period column's values lie in `[from, through]`.
- **Section 7.** With `through` declared, a longer window appends no period past `through`; without it, as before.
- **What moves.** An entry that declares either key is a different entry for `H` and the plan. An entry that
  declares neither writes the member it wrote, so every other bundle keeps its bytes.

## Amendment of 2026-11-24: `init` projects a table that is also a counted child

Section 9 left an expose that a `counts` entry lays out as a counted child unprojected, since a `counts` entry over
a relationship of a projected emit is `FLX-3403`. The table then kept its own rows, and a snapshot of the history
had a different number of rows per parent than the history had entities. `init` now writes the projection:

- **The projection.** An expose that meets a rule whole is projected whether or not the profile counts its children
  per parent. For a snapshot, an events or a periodic emit, a foreign key that a `counts` entry would have set must
  be a column of the history, from which the emit takes it, and the history must write a `parent` under the same
  parent expose, so that the column holds that parent's keys on every version. A history with no `parent` holds
  drawn values in the column, which are no keys of the parent, so a table that would copy them stays unprojected,
  with that as the part of the rule it misses, and keeps its own rows and its own `counts` entry.
- **The statistic.** The emit's own `counts` entry is not written. A snapshot has one row per entity, so its children
  per parent are entities per parent: when the history has a `parent` under the same parent expose and no `children`
  of its own, the entry's `children` become the parent's, and so does its `minChildren` when the parent has none.
  An events or periodic emit counts events or periods per parent, which is another unit, so its statistic is never
  given to the parent: it is unused, with that reason. A statistic is unused with its reason too when the history
  has children of its own. Where two snapshots of one history could give, the one whose emit id sorts first does,
  so the result does not depend on the order of the exposes. The ledger says which statistic moved where and why.
- **What moves.** A bundle for a contract with such an expose. Every other bundle keeps its bytes.

## Amendment of 2026-11-25: a budget on the rows of a periodic emit

`MOST_PERIODS` bounds the periods of an entry, but not its rows, and `plan` generates every row it plans: a monthly
entry over five thousand years for a thousand entities derives sixty million rows, which took gigabytes before any
check saw the count.

- **The estimate.** Before it lists a period, `plan` bounds the entry's pairs from the versions alone: a version valid
  over `[a, b)` inside the periods' span meets at most `(b - a) // shortest + 2` periods, `shortest` the fewest days
  a period of the grain spans (1, 7, 28, 89 or 365), and never more than there are periods. The estimate is that sum
  times the most rows a pair takes, over the kinds a count or the high end of the largest class. Spark's `sequence`
  sizes its array the same way, in exact arithmetic before it allocates. The estimate is never below the rows the
  layout makes.
- **A rare high count.** Since the estimate never counts fewer rows than the layout makes, a count class's high
  level is taken for every pair, however rare the class: a kind whose count is 1 almost always and 100,000 once in
  a hundred thousand pairs is estimated at 100,000 rows a pair. The refusal then names the kind and that level.
- **The budget.** An estimate above the budget is `FLX-3403`, naming the emit, the grain, the window, the periods,
  the pairs, the estimate, the rows a pair takes and the budget, and the two ways out: a smaller estimate (a lower
  high level where a count class sets it, else a narrower window or a coarser grain), or a larger budget. The budget
  is 50,000,000 rows a periodic emit; `--max-rows N` on `plan`, `run` or an `apply` that takes a fresh plan sets
  another, up to `MOST_ROWS`.
- **When it runs.** Only while a plan is made. A recorded plan states the rows it derives, so `apply --run-id` and
  `replay` run it with no estimate, and `MOST_ROWS` alone bounds its rows; `apply --run-id` refuses `--max-rows`.
- **What moves.** Nothing a plan makes. A budget other than the default is recorded in `plan.json` as `maxRows`; a
  plan with the default carries no key and keeps its bytes and its planDigest. A plan recorded before the budget
  replays as it did.

## Amendment of 2026-11-25: a `given` over a column of the version is drawn after the projection

The first amendment of 2026-11-24 refused a column of an events or periodic emit drawn `given` a column the entry
sets, because the draw read the level the other column's own draw made, which the entry then replaced. A bundle
that projects a column of the version (`model` on a month, or on a change) and draws another column given it
(`tier` given `model`) is a plain shape, and the refusal left it no way to say so. This amendment draws it:

- **The order.** The entry's cells are set first. A column of the version the entry sets, which some column of the
  emit is drawn `given`, gives each row the level the version holds, and the emit then draws its own cells: the
  column drawn `given` it, and every column drawn `given` that one, reads the held level in place of the level its
  own draw made. Each cell is still drawn once from its own stream, so the order changes no other value, and the
  column path, the per-row path, `--jobs` and `--shard` give the same bytes. A categorical column's level is the
  one that renders to the version's value; a column drawn from bins takes the bin that holds it.
- **What stays refused**, `FLX-3403` at plan, naming both columns and the entry. A column drawn `given` a cell the
  entry sets that is not a column of the version: the `op`, `at`, `date` and `type` columns, the period column, a
  kind's override, and the before and after images (an image is NULL where the event has no such version). These
  are not drawn from the column's levels, so a held level would name a level the column's table is not keyed by.
  A column with `allocation: exact` drawn `given` a column of the version, whose classes are allotted over the
  rows by the draw of that column, not by the level a row holds.
- **A value no level names.** The history may fill a column of the version with a value no level of the emit's
  column names: it declares a `nullFraction` on it, a rule, a relationship or the history's `end` sets it (an `end`
  value that is a level of the emit's column is served), it draws levels the emit's column does not hold, or, for a
  column drawn from bins, it is of another type. A row that holds such a value has no row in the given-table of a
  column drawn `given` it, so that column is drawn from the marginal the declarations imply: `P(D = d)` is the sum
  over the levels `c` of the parent `C` of `w_c * P(D = d | C = c)`, with `w` the level weights `C` declares for the
  levels `D`'s table is keyed by and `P(D | C = c)` the table's row for `c`, taken as shares. It needs no declaration
  of its own and fits nothing to data. The weights are read from the emit's `C`, and from the history's `C` where
  the emit's declares none (the emit's wins where both declare, since the table is keyed by its levels). The sum is
  exact (`Fraction`, the levels in sorted order, normalised), and the plan records the shares (`fallbackMarginals` in
  the generator document, beside `fallbackGivens`), so the digest covers them. Where `C` declares no level weights
  (a `support` or nothing), `D` is drawn from the `support` it declares, equal weights, which must name exactly the
  levels the table names, with a `maxCardinality` (a declared support is a checked property); a column that has
  neither, or draws from bins, which have no weights apart from the table's, is refused at plan, `FLX-3403` naming
  both columns and what to declare (level weights for `C`, or a support for `D`). Each cell is still drawn once from
  its own stream, and only the distribution read for that row changes, so the column path, the per-row path,
  `--jobs` and `--shard` give the same bytes. Rows whose value is a level of the emit's column are drawn from the
  given-table as before. The `end` source cannot be reached end to end today (every level the history ends in is one
  of its own levels, and every level of the history is a level of the emit's), so it is pinned at the plan alone.
  `plan` prints one note for each such column, naming the column, its parent, what sets the parent's unnamed values
  and which distribution is drawn. A `conditions` group cannot read such a column: conditions on a projected emit
  are refused at plan (`FLX-3643`).
- **Recorded plans.** A plan with such a column says `projectedGivens: "afterProjection"`, which the plan digest
  covers; plan.json states only that key. The generator document the plan's digest covers (its `generatorDigest`)
  names the held columns, as `heldGivens` in the projection member of the emit, and those of them that fall back
  to the column's own distribution, as `fallbackGivens` (absent when none does), with the shares of each marginal
  as `fallbackMarginals` (absent when none has one). A recorded plan without
  the key replays the draw engine 0.11.1 made, given the level the column's own draw made, so its rows and its run
  id keep their bytes. Any other value is `FLX-2102`.
- **What moves.** Only a bundle the first amendment refused: its plan is now accepted. Every other plan writes no
  key and keeps its bytes.

## Amendment of 2026-11-26: `init` bounds a periods entry by the months its column publishes

Section 9 wrote a periods entry with neither `from` nor `through`, so its periods ran from the window's start over
every month a version held, although the profile's histogram of the period column says which months hold rows. A
history with versions from many years back then wrote periodic rows in each of those years, far outside the months
the data covers.

- **What is read.** The period column's histogram (`marginalHistogram/1`), only when its one group is published and
  every edge is a whole day. `from` is the start of the period that holds its first edge, counted from the
  window's first day as the entry's periods are without it, so no period moves to another day; `through` is its
  last bin's start, so the last period is the one the last bin opens. A withheld histogram, or none, writes
  neither key, and no withheld `min` or `max` is read.
- **Held to the run.** Both are held to the window and to the history's `until`: a `from` on or before the window's
  first day is left out, as that day is the entry's anchor without one, and so is a `through` at or after the
  window's last day; a `through` past `until` is the last day before it. A histogram wholly outside them writes
  neither.
- **What moves.** A bundle `init` writes whose periods entry's column publishes such a histogram that starts after
  the window or ends before it. Every other bundle keeps its bytes, and no plan or recorded plan changes.

## Amendment of 2026-11-26: a `given` over a kind's override is drawn after the projection

The amendment of 2026-11-25 drew a column `given` a column of the version after the projection, and kept refusing a
column drawn `given` a column a kind of a periods entry overrides. A periodic emit whose kinds each set the row's type
(`charge` is `fuel` on one kind's rows and `toll` on the other's) and that draws a column given that type (`band`
given `charge`) is a plain shape, and the refusal left it no way to say so. This amendment draws it:

- **The level a row holds.** A column the entry sets, which some column of the emit is drawn `given`, gives each row
  a level before the emit draws its own cells: on the rows of a kind that overrides it, the override's; on the other
  kinds' rows, the version's for a column of the version, and none for a column of the emit's own, whose own draw then
  stands, its level with it. The column drawn `given` it, and every column drawn `given` that one, read the held
  level. A column one kind overrides is still drawn on the rows of the kinds that do not, so it too is drawn given
  the level those rows hold. Each cell is still drawn once from its own stream, so the column path, the per-row path,
  `--jobs` and `--shard` give the same bytes. A categorical column's level is the one that renders to the override; a
  column drawn from bins takes the bin that holds it, as its own draw would.
- **An override no level names.** An override that renders to no level of the emit's column has no row in the
  given-table, so on that kind's rows the column drawn `given` it is drawn from the marginal the declarations imply,
  `P(D = d)` the sum over the levels `c` of `w_c * P(D = d | C = c)`, as for a version's value no level names (the
  emit's level weights, else the history's for a column of the version), else from the `support` it declares,
  evenly, and the plan is refused without either, `FLX-3403` naming both columns, the kind and the value.
  `plan` prints one note for each such column, as it does for a version's value.
- **What stays refused**, `FLX-3403` at plan, naming both columns and the entry: a column drawn `given` the period
  column, and, on an events emit, `given` the `op`, `at`, `date` or `type` column or an image; and an override with no
  distribution to fall back to. A periodic emit declares no `allocation: exact` (its rows grow with its window), so no
  column allotted by its parent's draw reaches this step.
- **Recorded plans.** A plan whose draw holds a level engine 0.12.0 did not hold (an override, or a column some kinds
  override drawn given the level the other kinds' rows hold) says `projectedGivens: "afterOverrides"`, which the plan
  digest covers, and `heldGivens`, `fallbackGivens` and `fallbackMarginals` name what it holds, as before. A plan with
  `"afterProjection"`, or without the key, replays the draw it was made with, so its rows and its run id keep their
  bytes. Any other value is `FLX-2102`.
- **What moves.** A bundle the amendment of 2026-11-25 refused for a column drawn `given` an override now plans. A
  column some kinds override, drawn `given` a column the entry sets, was drawn on the other kinds' rows given the
  level of its parent's own draw, which the entry then replaced: it now reads the level the row holds, and a new plan
  of such a bundle says `afterOverrides`. Every other plan keeps its bytes.

## Amendment of 2026-11-26: `init` keeps the history when the profile counts rows per entity

A profiler counts a child's rows per parent over every relationship, so over a relationship whose `toColumns` are
a versioned expose's natural entity it counts the child's rows per entity. `init` wrote a `counts` entry over it,
which the plan refuses for a history (ADR 0017 section 8: no counted layout of the versions), so `init` wrote the
bundle again without its projections and then without its history; with no history the counted layout needed
each version's entity to be unique, and that was refused too.

- **No layout reads it.** A parent row of a table with a history is a version, which no `counts` entry, and no
  spread of the foreign key over the parent's keys, turns into an entity: a child picks a version as any child
  does. So a relationship into a table with a history that the counted layout would place is laid out by no entry,
  and each statistic of its profile entry is unused, with that reason: children per entity over the natural
  entity, per version over another key.
  A table has a history here when the history file declares one, or else when its profile table publishes one; a
  table with neither is counted as before. The history and its projections are written as they are without it.
  A relationship the counted layout cannot place is laid out as its foreign key's spread, as it always was.
  Only a history that is written holds a relationship back: when the plan refuses the history, or the profile's
  history is one `init` does not write, the bundle is planned again with that table's relationships counted, as
  they were before this amendment.
- **A projection says why in its own words.** When the child is an events or a periodic emit, the statistic is
  unused with the projection's reason: an event is one row per change, so its rows per entity follow from the
  history's versions; a periodic row is one row per version and period, and the profile counts the rows but not
  the periods each holds, so no rows per period are read from it. A note says it once per emit.
- **What moves.** A bundle for a profile with such a statistic into a table whose profile publishes a history,
  which `init` could not write with its history before. Every other bundle keeps its bytes, and no plan or recorded plan changes.

## Amendment of 2026-11-26: a link into a projected emit reads the rows the emit writes

A plan that reads another emit through a link (a rule that follows it, a `within` entry over it, a column drawn
`given` the linked row) carries that emit's plan, and the plan was attached before a projected emit's rows were
derived. A link into a snapshot or an events emit therefore read the emit's cells as it would draw them
without its projection, which are not the rows it writes: a `within` entry kept a date inside a lifetime read from a
cell the snapshot does not hold, and `verify` reported the entry met on fewer rows than it covers, against ADR 0024
section 5.

- **The link reads the projection.** Each plan that reads a projected emit, or reads a plan that does, carries the
  projected plan, so the linked row is the row the emit writes, rules included. The generator digest of such a plan
  names the projected plan's.
- **A summary reads it too.** A parent that summarises a child carries the child's plan, and the child carries the
  plans it reads, so a summary over a child that reads a projected emit is taken over the rows the child writes.
- **A history that reads a projected emit of another history.** The history carries the projected plan, and the
  snapshot, events or periodic emit projected from it is derived from the history's plan as it then is. Each plan that
  reads such an emit is renewed in turn, and the two steps go on in dependency order until the plans of every emit
  are the plans they were derived from. They settle because no history reads a projection of itself.
- **A history that reads a projection of itself is refused.** A history can link to the table projected from it, as
  a self-referencing table does: a version to the current row of another entity of the same history. The projection
  copies the history's columns, so the history would be derived from rows that are derived from it. Deriving
  it needs a derivation that follows the dependencies of each column, which the passes above are not: they compare
  whole plans, and a plan that reads its own projection moves with every pass or settles on rows that differ from the
  projection it read. The shape is refused (`FLX-3403`) before any pass, for a new plan, and a later change will
  design a derivation that follows the dependencies. The decision is made over the emits: each emit has an edge to every emit its plan reads or sums, whatever drives the read (a rule, a
  `within` entry, a column drawn `given` a linked row, a weighted pick, a `parentWhen`), and each projected emit has
  an edge to its history. A history from which a projection of itself can be reached is refused, which covers a read
  through another emit, a summary, and histories that read each other's projections. The message names the history
  and the emits in order, from the history to its projection, and says to read a drawn column of the history instead.
- **Recorded plans.** A new plan with such a link says `projectedReads: "final"`, which the plan digest covers. A plan
  without the key replays the link as it was made, so its rows and its run id keep their bytes. Any other value is
  `FLX-2102`.
- **What moves.** A bundle with a link into a projected emit that a rule, a `within` entry or a given column reads, a
  summary over a child that reads one, or a history that reads a projection of another history. A bundle whose history
  reads a projection of itself is refused when planned afresh, and a recorded plan of one replays as it was made. Every
  other plan keeps its bytes.

## Amendment of 2026-11-26: `init` starts a periods entry and limits an events entry where the data does

Section 9 wrote a `periods` entry with no start and an `events` entry that holds every version, so periods ran from
`timeBounds.start`'s day and an event was written wherever the data began. `init` now reads where the date column
lies:

- **The span.** A column's span is its contract's declared `range` (an end the contract leaves out is open, and a
  strict end leaves out its own instant), and for an end the contract does not declare, the profile's published
  span of the column: its minimum or maximum, or, for a column that publishes a histogram, the first and the last
  edge of the bins that stand clear of any noise. A column with neither has no span. Events read this span; periods read the
  declared range and the published histogram (below).
- **Periods.** Each end of a periods entry's range is the contract's declared end, else the first and the last bin of
  the column's published histogram (the amendment of 2026-11-26 that bounds a periods entry by the months its
  column publishes). A published minimum or maximum is not read for periods, as that amendment says. The entry's
  `from` and `through` are held to the run as that amendment holds them, and `from` is aligned the same way: the
  start of the period that holds the first day, counted from `timeBounds.start`'s day as the entry's periods are
  without it, so no period moves to another day. `through` is the last day. A column with no declared end and no published histogram writes neither key.
- **Events.** An event is dated at its version's start, since `init` writes no `at` or `lag`, so a `when` of
  `<start> >= DATE '<first>' AND <start> <= DATE '<last>'` over the history's validity start holds exactly the
  events inside the span, one bound for each end the window does not already hold. A history that writes an `end`
  dates a `d` event by a version's end, which no predicate on the version holds together with its `c` and `u`
  events, so its entries are not limited and the notes say what they would need. A validity start that is not a
  DATE writes no `when`. Events read a published minimum and maximum where periods do not, and the two differ on
  purpose. A periods entry writes a row for every period between its ends, so its range has to be the months its
  histogram says hold rows, and two extremes do not say that. An events entry writes no row of its own: its `when`
  only drops an event dated outside the span, and no event outside a column's published minimum and maximum can
  be one of that column's rows.
- **What moves.** A contract that declares a range on such a column, or a profile that publishes its span. Every
  other bundle keeps its bytes.

## Amendment of 2026-11-26: `init` keeps a snapshot whose foreign key a count would set beside another parent

The amendment of 2026-11-24 on a table that is also a counted child asked a history to write its `parent` over the
expose a counted foreign key reaches, so that the column holds that expose's keys on every version. A history is laid
out under one parent only, so a table counted per a second parent was left unprojected: it kept its own rows and its own
`counts` entry, and its keys and the history's entities no longer matched. `init` now reads what the column holds:

- **The rule.** A foreign key that a `counts` entry would have set is a column of the history, and it holds keys of the
  parent expose when the history writes its `parent` over that expose, or declares a to-one relationship of its own over
  the same column to the same key columns, and the history requires the column (a column that may be NULL holds no key
  in a row). The rule is the one a counted events or periodic table meets too. Such a column is picked from the parent's
  rows on an entity's first version and carried (ADR 0017 section 6, the carried columns), or tracked over levels that
  are keys of the parent, so every version holds a key. A history that declares neither has no key of the parent in the
  column, as before.
- **The statistic.** The `counts` entry is not written. When the history's parent is another expose, the statistic is
  unused, with a reason that names the table, the history and that expose, and no value; when the history writes no
  parent, or has children of its own, the reason says so, as the amendment of 2026-11-24 has it.
- **What moves.** A bundle for a contract with a snapshot, events or periodic table that is counted over a parent the
  history does not lay out its entities under, when the history requires the column and declares a relationship over it
  to that parent. Every other bundle keeps its bytes.
