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
