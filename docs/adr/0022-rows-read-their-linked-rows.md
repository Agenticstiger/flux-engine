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
