# ADR 0022: rows read their linked rows: copies, conditional links, sentinels, sibling windows and bridge pairs

## Status

Accepted 2026-11-21. Implements step 3a of
[ADR 0011](./0011-rows-follow-their-key-entity-and-parent.md) as its amendment of 2026-11-18 specifies it
(EXPR-SPEC-2, sections 5, 6 and 9), and adds four module facts under the namespace that
[ADR 0018](./0018-module-facts-live-in-the-flux-bundle.md) gives them. Steps 3b (the bounds form over a
relationship) and 3c (shares below 100 %) are not part of this record and stay `FLX-6210`.

## Context

FLUX generates the exposes of one contract together (KERNEL-SPEC section 8.2): every foreign key names a
row its parent holds, and nothing is materialised, so a slice of rows is the same in any process. A row
cannot yet read the row its key names. Four things follow from that, and each was reproduced on the
engine before this record:

- **Copies are drawn, not copied.** A relationship's `sharedColumns` and a column's `sharedAttribute` say
  that a child carries its parent's value (RFC-data-rules section 5). The plan lists both as declared and
  not generated. With 600 organisations, 1,800 accounts and `sharedColumns: [region]` over three levels,
  a child equals its parent on 34 % of rows under kernel 0.1.0 and 38 % under kernel 0.2.0, which is
  chance. The explicit form, `derive: region` with `expr: organisation.region`, is refused with
  `FLX-6210`.
- **A parent read is the parent's raw draw.** `RelationReader.view_value` returns what the parent's
  column draws on its own stream. It applies none of the parent's rules or dependencies, and a parent
  column drawn `given` another is refused when a relationship reads it.
- **The parent row is not exposed.** Which parent row a foreign key took is computed inside each pick
  (the nearest key under `declared`, a bounded draw under `uniform`, a permutation under `distinct`, a
  decoded point inside a several-column key) and then thrown away. A permuted identifier key cannot be
  inverted (`core/permute.py` has no inverse), so the row cannot be recovered from the value either.
- **What FLUID cannot state.** A link that exists only for some rows (a subscription has a promotion only
  when it was sold under one), the value a copy shows where no link exists, an order among the rows that
  share a parent (the first subscription of an account), and a table of pairs where no pair repeats (an
  enrolment of students in courses) have no FLUID spelling. EXPR-SPEC-2 section 5 specifies sibling
  windows as a FLUX extension; the other three are module facts in ADR 0018's sense.

Borrow-before-build search, 2026-11-21. Licences were read from each repository or its documentation
on that day.

| Name | What it does here | Licence | Taken |
|---|---|---|---|
| EXPR-SPEC-2 sections 5, 6 and 9 (this repository) | the meaning of a path, a copy, NULLs first and sibling windows | Apache-2.0 | the semantics, as specified |
| `flux_engine.expr2` (this repository) | the reference evaluator of paths, partitions and windows (`expr2/rows.py`, `Session`) | Apache-2.0 | reused: generation evaluates windows through it |
| DuckDB 2.0.0 (docs at `duckdb.org/docs/2.0`) | window functions: `RANK` "with gaps", `DENSE_RANK` "without gaps", `LAG` and `LEAD` offset 1 and default NULL, NULLS LAST in both directions, a default frame that ends at the current row's last peer | MIT | the test oracle, as EXPR-SPEC-2 section 11 already makes it; never imported by the engine |
| Snowfakery (docs, latest; repository archived 2026-09-18) | `random_reference` with `unique: True` "ensures that each target row is used only once", scoped per parent with `parent` | BSD-3-Clause | pattern only: a unique pair is a unique pick of one side per row of the other |
| SDV `ForeignToForeignKey` (docs) | one concept carried "consistent everywhere in the database" across tables | BUSL (SDV Enterprise add-on) | pattern only, as ADR 0003 requires |
| dbldatagen (docs) | `baseColumn` orders generation by the columns a column reads | Databricks License | pattern only: rules are ordered by what they read, as EXPR-SPEC-2 section 10 already says |
| Devart's foreign key generators (docs) | a child column filled from a parent column | proprietary | none |

No tool found keeps a parent row's own rules when a child reads it, or records a pick so that a permuted
key can be followed without an inverse.

## Decision

### 1. Kernel 0.2.0 only

Everything below generates under kernel `0.2.0`. A kernel `0.1.0` plan keeps every behaviour it has: a
`sharedColumns` or `sharedAttribute` stays declared and not generated, a rule that reads another row
stays `FLX-6210`, and the namespace keys this record adds (`links`, `bridges`, `copies`) are refused
with `FLX-3105`, naming the kernel. ADR 0010 keeps kernel `0.1.0` for replay with no new behaviour.

### 2. Every pick records its parent row

The reader records, for each row and relationship, the parent row its pick took, or none when the key is
NULL: the nearest key under `declared`, the bounded draw under `uniform`, the permutation under
`distinct`, and the decoded part under a several-column key. A row it has not recorded is computed the
same way, from the same streams, so the record is a cache and never a source. A relationship whose
columns another relationship sets (KERNEL-SPEC section 8.2, the inner of two relationships that set one
column) names its row through the outer one, and reading through it is `FLX-6210` in this version.

### 3. A path reads the parent's final value

A path reads what the target expose's own pass writes at the row the link names: its cells, its
dependencies and its rules, and a column drawn `given` another. The plan of a reading expose carries the
plan of every expose its paths reach, and of every expose their rules reach in turn. The reader makes a
reached row through that plan, the function the target's own pass runs, and keeps it for the slice. The
reading plan's document gains `reads`, the `generatorDigest` of each reached plan, only when it has one,
so a moved parent moves its readers and a plan with no read keeps every byte. A path through an expose
the simulation maps from its scenario, and a cycle among the exposes that rules read through, are
`FLX-6210`, as EXPR-SPEC-2 section 6 states.

### 4. Copies

- **Copies generate where the Simulation declares `copies: true`.** A contract that already lists a
  `sharedColumns` or a `sharedAttribute` was read as declared and not generated, so a kernel `0.2.0` plan
  written before this record replays to its digests only if its bundle reads the same way. KERNEL-SPEC-2
  section 12 makes a change additive only when a new declaration reads it, so the copies below generate
  only for a Simulation that carries the top-level key `copies: true` in its namespace
  (`extensions['com.agentics.flux/1'].copies`). Its only value is `true` (anything else is `FLX-3105`), it
  is bound into the `H` of each emit that lists something to copy, as `links` is bound, and `G` leaves it
  out. Without it every bundle behaves as it did before this record, under both kernels: the two
  declarations stay listed as declared and not generated with no reason, no copy rule is planned, and
  the plan documents, the coverage, `column-provenance.json`, `fluxsim verify`'s `rules` lines and every
  digest are the ones the bundle always had. With it under kernel `0.1.0` the plan is refused with
  `FLX-3105`, naming the kernel. `fluxsim init` writes the key, and only for a contract that declares a
  `sharedColumns` or a `sharedAttribute`.
- A contract `derive` that reads `<relationship>.<column>`, and a FLUX-side `derive` that follows a chain
  of to-one relationships, generate as EXPR-SPEC-2 section 6 says, NULLs first.
- A `sharedColumns` entry is the copy `derive: <column>`, `expr: <relationship>.<column>`, on every row. A
  row whose link reaches no row takes NULL, or the link's sentinel (section 6 below).
- A `sharedAttribute` is drawn on its owner, the member every other member reaches on exactly one path of
  relationships, and every other member copies it along that path. No owner, or two paths to it, is
  `FLX-6210`.
- A copied column needs no draw of its own, since the copy covers every row. One that declares a
  `nullFraction` above zero keeps its own NULLs first, and each counts as a violation of the copy, which
  allows none, so the plan refuses it in every run (`FLX-6204`), as it refuses a contract or FLUX-side
  `derive` over a path in the same case. A rule with a `rows` band that admits violations keeps its
  NULLs first. `fluxsim init` writes no draw and no `nullFraction` for a copied column.
- A copy into a key or a foreign key column waits for EXPR-SPEC-2 section 6's copy of a foreign key: a
  `sharedColumns` or `sharedAttribute` member that is one stays declared and not generated, and a rule
  that determines one stays `FLX-6210`.

### 5. Conditional links

The namespace key `links` lists one entry per relationship it configures:
`{emit, relationship, when, sentinels, weight}` (`weight` is ADR 0023's). `when` is a FLUID SQL/1
predicate over the emit's own columns. On a row where it is not TRUE the relationship's columns are NULL,
so no link exists there; on any other row the foreign key is what it drew. The pick is still drawn on
every row, so no draw moves. The step that clears the columns is ordered with the rules: it reads the
predicate's columns, and every path through the link reads the link's columns, so a copy through a
conditional link sees the NULL. A condition on an `inner` relationship, on one whose columns are a key
or part of one, or on one another relationship implies, is `FLX-3403`.

### 6. Sentinels

A `links` entry's `sentinels` maps a column the entry's relationship copies (a `sharedColumns` column of
it, or a `derive` whose whole value is a path through it) to the text of its sentinel, read as the
column's type. On a row whose link reaches no row the copy writes the sentinel in place of NULL. A row
whose own value is NULL keeps it. A sentinel that does not read as the column's type, or names a column
the relationship does not copy, is `FLX-3105`. So is a sentinel on a copy whose `derive` has a `when`: a
row that fails the `when` keeps a drawn value, so the copy could not verify.

### 7. Sibling windows

A FLUX-side `derive` whose value holds a window over siblings (EXPR-SPEC-2 section 5) generates. The
partition is the rows of the expose whose relationship columns are equal, the rows with a NULL link
forming one partition, so it is a function of the rows alone. An expose with such a rule is made whole in
each process that writes a slice of it: its cells and the rules ordered before the window for every
row, then the windows over the whole table, through `flux_engine.expr2`'s evaluator with one session per
rule, then the rules after them. The table is kept for the process and each slice is cut from it, so the
bytes are the same for any `--jobs`, chunking or `--shard`. The cost is one pass over the expose per
process that writes it, which the documentation states. A window's order must be total where ties
decide its value (`FLX-6108`).

### 8. Bridge pairs

The namespace key `bridges` lists `{emit, relationships: [a, b]}`: two many-to-one relationships of one
emit whose pairs never repeat. They are laid out together as one key of two parts (KERNEL-SPEC section
8.2), the point `permute/2(k, rows_a * rows_b, rowIndex)` decoded in mixed radix, `a` first, with `k` the
layout key of the label `("sim", sim_id, "gen", exposeId, "pair", a, b)`. So no pair repeats, and each
side names a row its parent holds. More rows than pairs, a `nullFraction` on either side, a relationship
that is already distinct or part of a key, and a condition or a weight on either, are `FLX-3403`.

### 9. Checks

`fluxsim verify` scores each one on the run's rows, as a line of its `rules`: every copy (`copied` by
`rule`), every conditional link (no link where its condition is not TRUE), every sentinel (every row with
no link holds it) and every bridge (no pair repeats). Each is enforced, so a violating row fails the run,
as a `derive` without a band does.

## Consequences

- A bundle that declares none of this keeps every byte and every digest. No seed-tree path is added:
  copies, conditions, sentinels and windows draw nothing, and a bridge's layout key is derived from `H`
  under a label no other layout has (KERNEL-SPEC-2 section 12).
- A kernel `0.2.0` bundle that declares `sharedColumns` or `sharedAttribute` keeps its rows and digests
  until its Simulation declares `copies: true`: the key is a new declaration, and KERNEL-SPEC-2 section 12
  makes a change additive only when a new declaration reads it. With the key its copying exposes' rows
  change, which is a new run of a new bundle. A golden pins the bundle both ways, with the digests
  origin/main gives for the first, and the changelog states the key.
- A reached parent row is made again by each process that reads it. Parents are usually smaller than
  their children and each row is made once per slice, so the cost follows the distinct parent rows a
  slice names.
- The column path of kernel `0.2.0` keeps drawing the cells. Rules that read another row are applied a
  row at a time, so its rows are the per-row path's, which a test compares.

## What would change this decision

- A copy of a foreign key, or a path through the inner of two relationships, when a contract needs one.
- A window table too large for one process, which would need a layout that puts siblings on
  consecutive rows.
- FLUID gaining a spelling for a conditional link, a sentinel or a pair key, which would move the
  declaration into the contract.

## Amendment of 2026-11-24: a copy into a foreign key sets that relationship's pick

Section 4 left a copy into a key or foreign key column out. A child with two relationships, `R` to a
parent that holds a second parent's key (a trip's vehicle holds `depot_id`) and `S` to
that second parent (its depot), drew `S`'s foreign key on its own, so the two disagreed. A copy of the
column through `R` into `S`'s foreign key now sets `S`'s pick.

The rules, each decided here:

1. **Opt-in.** Only a `sharedColumns` entry of `R` that lists a column which is the one-column foreign
   key of another relationship `S` of the same expose changes anything, and only where the Simulation
   declares `copies: true` on kernel `0.2.0`. Any other copy, every bundle without `copies: true`, and
   kernel `0.1.0` (where the declaration stays listed as declared and not generated) keep every byte.
   A column that is a key, part of a key, or `R`'s own foreign key stays listed as before.
2. **The pick.** The row `S` reaches is the row of `S`'s target that the parent of `R` reaches through
   its own relationship `Q`: `S`-parent(row) = `Q`-parent(`R`-parent(row)), and NULL when either reaches
   no row. The foreign key column holds that row's key, so it equals the copied value, and a link, a
   derive over `S.x` or a copy through `S` reads that row. The copy draws nothing and makes no rule.
3. **Integrity.** The copy is accepted only when the source column is itself the one-column foreign key
   of a relationship `Q` of `R`'s target to `S`'s target over the same key columns: the value is then a
   key of `S`'s target by construction. Otherwise the plan is `FLX-6210`, naming the column, both
   relationships and why. A NULL source gives a NULL foreign key, so `S` with `joinType: inner` is
   `FLX-6210` unless `R` and `Q` are inner too.
4. **What `S` may not be** (`FLX-3403`, the codes of the other relational refusals): one that references
   each parent once (one_to_one, or its column a key), part of a key or a bridge, counted (a `counts`
   entry or an expected-count hook), weighted by a `links` entry, a history's `parent`; a column that
   declares a `nullFraction`, a draw, an `allocation` or a `given`, or that a rule sets; and `R` with a
   conditional link or a rule on its columns, or `Q` the same in `R`'s target, since the row then
   reached is not the pick. `R` and `Q` must have a pick of their own; `R`'s target must not be a
   reference expose. The foreign-key column of `R`, and the column `Q` holds in `R`'s target, must be
   neither drawn `given` another column nor read by another column's `given`, since a pick another
   column's level fixes is not read from the row's cell alone; `Q` must be laid out as a foreign key (or a
   part of a key) of its own in `R`'s target. Each is `FLX-3403`, naming the column and the relationship.
   `R`'s target must also declare no history and be no projection of one (`FLX-3403`, naming `R` and `S`):
   an entity's foreign keys are those of its first version on every version, so `Q`-parent(`R`-parent(row))
   would not equal the copied cell on a later version; and a row of a snapshot, events or periods emit holds
   the cells of the version it reads (ADR 0027), not the pick of a draw of its own, so the row `Q` reaches
   from it is not the row its cell names.
5. **Order and cycles.** A pick is a function of other picks, not a cell a rule sets, so nothing waits
   for a rule: a rule, link or copy that reads `S` reads the pick. The plans read each other through `R`
   as a path does, and a cycle among them is `FLX-6210` as a cycle among rules that read links is. A copy
   through a copy (`Q` itself copied) follows the same rule one level down.
6. **Determinism.** No search over keys is made: the row is found by following picks, each a pure
   function of the plan and the seed tree, so the rows are the same under any `--jobs`, slice, and on the
   column path and the per-row path (the column path takes this pick per row, as a weighted pick does).
   The cost is two picks per row and the parent's cells; no index is built and no memory is held beyond
   the existing pick caches.
7. **Reports.** `plan` lists nothing as declared and not generated for the column, coverage names the
   relationship as its source, and `fluxsim verify` scores the copy as the `derive` it is (`R.column`),
   next to the relationship's integrity line, which keeps holding on every row.

A contract's own `sharedColumns` copies nothing from a slowly changing dimension through a relationship,
so a slowly changing target, and a projection of one, are out of reach of this amendment, and a copy through
either is refused (point 4).

## Amendment of 2026-11-26: init writes no counted layout and no draw for a pick a copy sets

### Context

A copy into the foreign key of another relationship `S` sets `S`'s pick, and a copy sets the pick alone
(rule 4 of the amendment of 2026-11-24): the plan refuses a `counts` entry over `S` (`FLX-3403`, "it is on
the counted layout") and a draw that `S`'s column declares. `init` read a profile's children per parent for `S`
as it reads them for any relationship, wrote a `counts` entry and left the key declared by its own statistics,
so a contract that copies a foreign key had no bundle that `init` could write: it refused its own.

### Decision

1. **The contract's copy stays.** Where the Simulation copies (`copies: true`, kernel `0.2.0`) and a
   `sharedColumns` entry lists the one-column foreign key of another relationship of the expose, `init` keeps
   the declaration and writes nothing that sets the pick a second time. The contract declares the copy, and
   the profile's statistics describe its outcome without choosing it: a copy makes the child's key equal to
   the key of a row it reaches, which no count of children per parent could keep, while the counts are
   what the copy's keys give the parents on their own.
2. **No counts entry, no draw.** The relationship is laid out by no `counts` entry, and its foreign key
   declares no levels, bins, range or null share, as a key a layout sets declares none. The relationships are
   found as the plan finds them, by one function (`flux_engine.seam.keys.copied_pick`).
3. **No exposure either.** A `within` exposure over the relationship (ADR 0025 section 4) counts it too, so
   none is declared for it: its statistics are unused, with a reason that names the relationship the copy goes
   through and no value.
4. **What the profile's statistics say.** Every statistic of the relationship's profile entry is unused, with
   a reason that names the relationship the copy goes through and no value, and the statistics of the key's
   own column are passed over as they are for any key a layout sets.
5. **What does not change.** A relationship no copy sets, a copy of a column that is no foreign key, a
   copy of the relationship's own foreign key, and every bundle without `copies: true` keep their bytes.
