# Changelog

What changed in the **flux-sim-engine package**, release by release, and in
**flux-stats**, which is released with it from the same tag at the same version
(ADR 0009).

This is not `GOLDEN-CHANGELOG.md`, which sits beside it at the repository
root and records something else entirely: every deliberate move of a pinned
digest or golden baseline. The two are easy to confuse and answer different
questions. Ask `GOLDEN-CHANGELOG.md` why a digest changed. Ask this file what
a given released version contains.

`scripts/release_guard.py` refuses a tag whose version has no `## X.Y.Z`
section here, so the entry lands in the same pull request as the version
bump and is reviewed like any other change. See `docs/RELEASING.md`.

Versions here are **package** versions. They are not `KERNEL_VERSION`,
`SEED_TREE_VERSION` or `TRACE_VERSION`, which version normative engine
behaviour, are hashed into `inputDigest`, and never move as part of cutting
a release.

Headings follow `## X.Y.Z - YYYY-MM-DD`.

## Unreleased

- `fluxsim init` reads `quantile/2`, the binned quantile `fluid profile` publishes
  by default: each group is two points of a number, date or timestamp column's
  distribution, and the bins are laid over them. A withheld `min` or `max` beyond
  such a point is closed by a maximum-entropy tail with the published mean, or by
  the next bin of the profile's ladder, and each is an assumption. A published
  `distinctCount` is checked against the bins' span.
- `fluxsim init --contract --profile` keeps a ledger of the profile (ADR 0014):
  every statistic and metric, by its JSON Pointer, is used, unused with the
  reason no rule reads it, or withheld. The command prints the counts and every
  unused statistic, and the Simulation lists the unused ones under
  `extensions['com.agentics.flux/1'].unused`, beside the register. `--json`
  carries the whole ledger and the gaps.
- Parameter register format 1-2-0 (PARAMETER-REGISTER-SPEC section 1.1): a
  Simulation may list the statistics of its pinned profile that no number rests
  on, each with its reason. `plan` reads each citation in the profile
  (`FLX-3113`), the record carries the list, the `parameters:` line counts it,
  and `EVIDENCE.md` names each one under "What Was Not Proven". A register that
  lists none keeps its format 1-0-0 or 1-1-0 record, bytes and digest.
- `fluxsim init --strict` writes nothing, and exits 1, when a statistic of the
  profile is unused or an assumption stands in for something the profile did
  not publish, a gap. `--allow POINTER`, repeatable, accepts the
  findings at or below a profile pointer or a Simulation pointer, and one that
  accepts nothing is refused too.

- `fluxsim init` never draws a column as one value unless its profile shows one
  (ADR 0014). A lone published `min` or `max` is a bound: with a mean it is
  filled by the distribution of largest entropy from it, a geometric tilt over
  bins found in exact fractions, whose mean as drawn is the published one; with
  `distinctCount` alone, that many grid values from it. A mean with no spread
  and no bound is filled from 0 on its side. A column with nothing published,
  or with values beyond what a row carries, is a placeholder spread rather than
  0 on every row, and a number foreign key with no spread of its own is drawn
  over its parent's keys, never under one parent.
- A declared level that a level histogram does not list, and a withheld cell,
  share what the published counts leave of the group's `k`; a cell withheld as
  small takes no more than half the minimum cell. The 0.7.0 rule, the mean of
  the published cells, holds only when the group publishes no `k`. A histogram
  whose levels are a subset of the contract's `enum` now weighs the draw, and the
  message names the levels it did not list.

- `fluxsim init` reads grouped statistics (ADR 0014 section 4). A level or
  marginal histogram grouped by a categorical column is a two-way table; each
  is scored by its mutual information, and the Chow-Liu forest under the
  generator's rules decides which column is drawn `given` which. The child
  declares `levelWeightsGiven` or `binWeightsGiven` from the group counts, a
  withheld cell filled from its group's `k`, and a grouped property the run is
  scored on. A grouping the forest leaves out is unused in the ledger, with the
  reason. `calibrate.structure.spanning_forest` is the selection step of
  `suggest_tree`, callable with scores of the caller's own.
- A categorical column is written with no assumed `nullFraction`, and none for a
  published null count of zero: an absent one draws no null, and a declared one
  kept the column from being drawn on (`FLX-3402`).

- `fluxsim init` reads a profile's `relationships` (ADR 0014 section 5). For a
  one-column number foreign key the contract declares, the classes of a
  children-per-parent histogram are laid over the parent's key range, each a
  share of the keys equal to its share of the parents (the Largest Remainder
  Method) and weighted by its parents times their mean number of children, the
  class of none at 0; the open class's mean is solved from the relationship's
  mean. With a histogram of the foreign key's own values the classes are laid
  inside each of its bins, so both hold. A mean alone leaves the spread even.
  The approximation is documented: about `e^-m` more parents of a class of mean
  `m` receive no child.

- `fluxsim init` never draws a column a contract `derive` sets, and never gives
  it its own profiled range, which made `verify` refuse the run (`FLX-5005`)
  once a property read the column (ADR 0014 section 6). Its range is the exact
  bounds of the rule over the ranges of the columns it reads, an interval in
  fractions over the expression's syntax tree (`+`, `-`, `*`, `/` away from
  zero, `ROUND`, `COALESCE`, `CASE`), or its published histogram's edges when
  they hold those bounds, widened to the column's own published `min` and `max`
  when they lie beyond, since the check reads those against the range too; its
  published mean becomes a property the rule's values are scored on. Bounds
  pass down a chain of rules, and a guarded `derive` leaves its column drawn.
- A column a contract `dependency` sets gets no `mean` property from `fluxsim
  init`: the run draws it once for each value of the columns it follows, so
  its mean rests on those few draws, and a tenth of the mean is no margin.
- A DATE column is spelled one way. Its fidelity `range` and `bins` can be
  `YYYY-MM-DD` (a whole number of days since 1970-01-01 still works), where a
  fidelity block that spelled them as dates was refused at `fluxsim validate`
  with `FLX-5022`. `verify` reads the column's rows as dates, so a value
  outside its range is refused as it is for an INT. A DATE column bounded by
  RFC 3339 instants reads them as before, as a timestamp column in
  milliseconds, so a block a profiler declares with instants still reads.
- A profile holds a DATE column as a timestamp in milliseconds, and `verify
  --profile` now scores a mean, a marginal histogram and a quantile over it
  against that profile, where it refused every such property with `FLX-5037`.
  The check converts the profile's numbers to days exactly and refuses one that
  is not a whole day. The shape flags read a `YYYY-MM-DD` value against a
  profile's timestamp column. No profile format, digest or golden baseline
  changes.
- `fluxsim init` spells a DATE column's bounds `YYYY-MM-DD`, and a DATE column
  now carries the properties a number or a timestamp does, scored against the
  profile's dates.
- The scorecard states how much of the contract its score speaks for. Per table
  and for the run, `coverage` counts the contract's columns that carry at least
  one scored property, and the relationships the contract declares (the child
  expose's `semantics.relationships`, FLUID 0.7.7) that the profile constrains:
  its `relationships` list holds an entry for that parent and child with at
  least one published statistic. No property scores children per parent yet, so
  a relationship is counted as constrained, not scored. The overall line of
  `verify`, `ship` and `EVIDENCE.md` prints it with the score, for example
  `scorecard: overall 0.93 against profile sha256:... (covers 12 of 20 columns, 0 of 3 relationships)`,
  and each table's line ends with its own counts. A table with no fidelity
  block records no contract, so it is left out of the run's coverage and the
  line says how many tables are.
- The `scorecard` block of a Simulation may floor the coverage:
  `coverage: {columns: "0.8", relationships: "0.5"}`. A run below a floor is
  withheld as a score below a threshold is, and the plan records the floors
  beside the thresholds as `coverage.columns` and `coverage.relationships`.
  A plan whose Simulation declares none keeps its bytes and its `planDigest`.
- The scorecard record is format 1-2-0, which adds `coverage`. Its schema is
  `docs/spec/scorecard-1-2.schema.json`; `docs/spec/scorecard-1.schema.json`
  stays as published for the documents of formats 1-0-0 and 1-1-0, and a test
  pins its bytes. No digest of a bundle, a plan or a golden moves.
- `fluxsim plan` names what a bundle declares and the run does not
  generate. Three declarations are accepted and were drawn as if absent: a
  relationship's `sharedColumns` (and a column's `sharedAttribute`), a `check`
  that determines no column (a contract `check` with no `generates` setting, or
  a FLUX-side `check`), and a `check` that reads another row through a
  relationship. `plan` names each per expose after the coverage lines and in a
  `declared_not_generated` artifact, the evidence report lists them under "What
  Was Not Proven", and `--strict-coverage`, or `strictCoverage: true` in the
  Simulation's extensions, refuses the run before any row exists with the new
  `FLX-3305`, which says what is not generated and what to do. `ship` works the
  list out again from the bundle, so no plan, record or digest of any bundle
  moves.
- Migration: a Simulation with `strictCoverage: true` (or a run with
  `--strict-coverage`) whose contract has a `check` with no `generates`
  setting, `sharedColumns`, `sharedAttribute` or a check over a link planned
  before and is now refused with `FLX-3305`. Drop strict coverage, or the
  declaration, or set `generates` on the check.
- `e2e/roundtrip` is a local round trip against a generated truth, run by hand with no
  cloud. It draws three related business-to-business tables from a contract and
  a bundle it knows, profiles them with `fluid profile`, writes a bundle back
  with `fluxsim init --contract --profile`, runs it with `--verify`, and judges
  the copy against the truth: the scorecard and its coverage, a k-marginal
  score, childless and top 1 % shares per relationship, grouped shares and
  Cramér's V, the contract derives, init's unused statistics and any `CONSTANT`
  shape flag. Each target prints PASS or FAIL, and `--record` appends one
  line to a committed `history.jsonl`. It is its own uv project and no CI job
  runs it.
- `fluxsim init` keeps a column inside the range its contract declares: a
  `validationRules` entry of type `range` (`between A and B`, or `>=`, `>`, `<=`,
  `<` joined by `and`) bounds the bins, the levels and the maximum-entropy tail of a
  drawn number column (the check's range and a property's bins keep the profile's
  edges), and a column the profile publishes nothing for is drawn
  evenly over it instead of a placeholder.
- `fluxsim init` reads a key's own statistics: with its `min` or `max` withheld the
  keys are centred on the published mean, a `count` or `distinctCount` equal to the
  rows is checked as one value per row, and a published mean, `min`, `max` or `quantile/2`
  is checked against the even layout of the keys, unused with the reason when it
  disagrees.
- `fluxsim init` reads the statistics of a foreign key that a relationship gives a
  spread: its binned quantiles cut the parent's keys when it has no histogram, its mean
  is read from `sum` and `count` and a `mean/1` that agrees, and a published `min` or
  `max` is checked against the parent's key span.

## 0.7.0 - 2026-11-19

flux-stats exports the read of a FLUX bundle that the engine takes its digests
over, so forge-cli's profiler can read a Simulation the way the engine does,
and that read is now bounded. A quantile can be published as the bin that
holds it, so a profile can answer a quantile property on a continuous column.
A bundle within the bounds reads to the same documents as before, a profile
that ran before compares as it did, and no digest or golden moves. The engine
now needs flux-spec 0.5.4, which reads a bundle's files as UTF-8 whatever the
locale, as the engine's own pre-reads of them do.

### Added

- `docs/spec/profile-1-0.schema.json` keeps the profile schema of format 1-0-0, unchanged, beside format
  1-1-0 (`docs/spec/profile-1.schema.json`), as the parameter register keeps both of its formats. A profile
  written before this release names the 1-0-0 `$id` in its `_schemaURL`, so that id still resolves.

- FLUX reads FLUX-side rules from a bundle, at `extensions["com.agentics.flux/1"].emits[<exposeId>].rules`
  (a new per-emit key, EXPR-SPEC-2 §1), and generates from the `derive` rules among them whose reads stay
  in the row, in flux-expr/2 or flux-expr/3 (ADR 0011 step 1, ADR 0013). Their ids are unique across a
  Simulation's emits (`FLX-6201`), a column has one source (`FLX-6203`), and the columns of the contract's rules and the
  FLUX-side rules are set in one order (`FLX-6208`), so a calendar's day of the month, first day of the
  month, same day a year back and quarter name follow the date. A FLUX-side rule that reads `previous`, a
  path, children or siblings, and a FLUX-side `check` that would generate, are `FLX-6210`. Coverage names
  such a column's source `bundle.rules.<ids>`, and the plan records each rule with its origin and language.
  A bundle that declares no FLUX-side rule keeps every byte and digest.
- FLUX generates a contract's one-way `dependency` (ADR 0011 step 2, EXPR-SPEC-2 section 8): each key gets its
  `to` values once, on a stream keyed by the key, and every row of the key copies them, whatever the slicing or
  `--jobs`, under kernel `0.1.0` and `0.2.0`. A `to` column that is a single foreign key draws the foreign key's own
  statistics on that stream and takes the parent's nearest key, so it always names a row the parent holds. A
  dependency with `bothWays` or a band with a lower bound above zero, a `to` column that is a key, a composite or
  one-to-one foreign key, an identifier, a column drawn `given` another or one with no draw that is not a foreign
  key, a column another is drawn `given`, and an emit with no `fidelity` block are `FLX-6210`; a column another
  rule also determines is `FLX-6203`, and a cycle through rules and dependencies is `FLX-6208`. The band decides a
  `to` column's NULLs. No band or zero violations draw them per key, so a key's rows agree. An upper bound draws
  them per row, on NULL rows: a share of the covered rows the plan reads off the band, so that every column keeps
  its declared null fraction while the share of rows the NULLs make violate stays four standard deviations inside
  the band, as on a source whose NULLs fall on the same lines; an emit with no declared row count takes the fewest
  violating rows for a `percent` band, and a `rows` band there, or any upper bound in a strict run, is `FLX-6204`
  until the emit declares its `rows`. A band below what the column with the most NULLs can give alone, or below
  zero, is `FLX-6204` in every run, and so, in a strict run, is one that leaves less than the four deviations above
  it. A dependency none of whose `to` columns declares a NULL takes no NULL rows, whatever its band. The plan records
  the share as the dependency's `nullRows`. Coverage names such a column `derived` by `dependency`, with the source `contract.invariants.<id>`,
  and the plan records each dependency with its id, `from`, `to` and origin. `fluxsim validate` builds the
  generator plan of an emit whose rules determine columns, so it refuses what `plan` refuses about those columns.
  A bundle with no dependency keeps every byte and digest.
- The model FLUX checks rule texts against now knows which exposes have versions, so a contract text whose
  path crosses a relationship into an expose with a validity window is `FLX-6106`, as RFC-data-rules.md
  section 2.2 and EXPR-SPEC-2 section 3 say. It used to pass, because the model left versions out.
- Kernel `0.2.0` runs beside kernel `0.1.0` (ADR 0010, step 3;
  docs/spec/KERNEL-SPEC-2.md). `fluxsim plan --kernel 0.2.0` plans a run under
  it, and `apply`, `replay`, `verify` and `ship` run the kernel the plan names.
  Its streams are Philox4x64-10, keyed by a path with its ordinals left out and
  counted by those ordinals, a draw index and an attempt. A bounded draw takes
  Lemire's test on the draw's own attempts, and `poisson` searches integer
  thresholds of kernel `0.1.0`'s sums. The truncated normal and the exponential
  are binary64, every operation written out and none fused. Generated
  identifiers and `permute/2`'s round function come from Philox, and key layouts
  are labelled `flux-key/2`. `flux_engine.core.kernel2` holds it, and
  `tests/kernel2/test_engine_conformance.py` holds it to the vector files. A
  process that flushes subnormal results to zero is refused before a kernel
  `0.2.0` run draws anything (`FLX-5050`). Kernel `0.1.0` stays the default and
  byte-identical: none of its digests or goldens moves. The new golden set
  `tests/golden/related-star-v2/`, and the determinism manifest's new `run2.*`
  and `kernel2.*` digests, each have a `GOLDEN-CHANGELOG.md` row.
- `flux-expr/3`, the expression language of FLUX-side rules with a date part, date
  arithmetic and string concatenation (ADR 0013, accepted, and EXPR-SPEC-3). It is
  flux-expr/2 plus `EXTRACT(YEAR|MONTH|DAY FROM d)`, `ADD_DAYS`, `ADD_MONTHS` and
  `ADD_YEARS` (a month or year move clamps the day, and a result outside 0001 to
  9999 is NULL), and `a || b` over strings and integers (a NULL operand gives NULL).
  `flux_engine.expr2.parse` takes `language="flux-expr/3"`, and a text that names no
  language is flux-expr/2, whose texts, canonical forms and vectors are unchanged.
  The new forms are `FLX-6103` under flux-expr/2 and in a contract's text, a
  concatenation derived into an enum column is `FLX-6110`, `check_language` accepts
  `flux-expr/3`, and a flux-expr/3 rule that uses none of the seven extensions is
  `FLX-6209`. A bundle's FLUX-side rules are read in either language (see the
  FLUX-side rules entry above), and a bundle that declares none keeps every byte,
  digest and golden.
- `flux_stats.canonical` is the canonical form a digest is taken over
  (KERNEL-SPEC §2.1), standard library only. `flux_stats.exact_yaml` is the
  exact read of a bundle's YAML text, behind a new `yaml` extra
  (`pip install 'flux-stats[yaml]'`, which adds PyYAML). It reads text and opens
  no file. `import flux_stats` loads nothing outside the standard library.
  `flux_engine.core.canonical` and `flux_engine.seam.resolve` re-export them,
  and `FLX-1001`, `FLX-1003` to `FLX-1007` and `FLX-3105` move with them.
- A `quantile` fidelity property may declare `bins`, under a
  `marginalHistogram`'s rules. It is then formed and compared by a new
  statistic, `quantile/2` (ADR 0012): a group states the declared bin that
  holds the order statistic and the records below and through that bin, never
  the order statistic itself, and the check passes when the synthetic order
  statistic lies within the margin of that bin. The scorecard scores the
  distance from the bin. A quantile without bins is `quantile/1`, unchanged.
- Profile format `1-1-0` carries `quantile/2`. The reader reads formats
  `1-0-0` and `1-1-0` (`flux_stats.profile.SCHEMA_URL_1_1_0`), and refuses a
  document that names `1-0-0` and holds a `quantile/2` statistic.
- `flux_stats.definition_for_property` picks a property's registered
  definition from its declaration, and `definition_for_kind` takes `binned=`.
- `flux_stats.edge_counts.check_edge_counts` applies ADR 0012's publication
  rule to every statistic of a column that counts records between edges
  (`marginalHistogram/1`, `levelHistogram/1` over its declared levels,
  `quantile/2` and `quantile/1`), so a profiler implements none of its own:
  - a quantile group's cells below, in and above its bin, or its witness, each
    hold at least `minimumCell` records, or none at the ends of the range (for
    a `quantile/1`, none only where zeros are not disclosive);
  - the column's statistics share one partition, and a `quantile/1`, which
    counts at its witness, is published only as the column's one such
    statistic;
  - a group of too few records, or of none where zeros are disclosive, is
    withheld with every group of its groupBy;
  - more is withheld until no cell a reader can form by adding or subtracting
    is small, a cell of no records included where the rule holds zeros
    disclosive (`zeros_disclosive`);
  - only then are the counts rounded.
  It refuses counts that cannot describe one set of records, such as groups
  that do not add up to the column.
- A count table's withheld groups and cells (`marginalHistogram/1`,
  `levelHistogram/1`, `quantile/2`), and a `quantile/1`'s withheld groups, are
  all published `C`, whatever withheld them (PROFILE-SPEC §3, ADR 0012): an `A` told a reader that a cell holds
  fewer than `minimumCell` records, which a known total could turn into the
  count. `check_edge_counts` publishes only `F` and `C`, and returns the reason
  for the profiler's report. A profile that marks a count table's cell `A` or
  `D` still reads.

- FLUX generates columns from a FLUID contract's rules (EXPR-SPEC-2 §6) for the
  two forms whose value is a function of the row alone: a `derive` with `expr`
  that reads only the row's own columns, and a `check` of difference form over
  days whose column the Simulation names under
  `extensions["com.agentics.flux/1"].emits[<exposeId>].invariants.<id>.generates`.
  Determined columns are set after the row's own cells are drawn, in the order
  their rules read each other, and the coverage report and
  `column-provenance.json` name them as derived by `rule`. Every other rule that
  would determine a column is refused at plan time with the new `FLX-6210`, and
  `FLX-6202`, `FLX-6203`, `FLX-6204` and `FLX-6208` join the catalog. A bundle
  whose contracts declare no rule keeps its bytes and every digest; a rule of any
  kind, a check included, is part of its contract, and the plan of an emit that
  generates its rows records it.
  `fluxsim init`'s pre-flight reads the rules as `fluxsim run` does.

### Changed

- The seam's pre-check, the evidence packer's archive pre-read and the position a broken
  document's message names decode a bundle's files as UTF-8, whatever the locale, as flux-spec
  reads them from 0.5.4. They mirror its read and decoded in the locale's encoding, as flux-spec
  0.5.3 does, so under a locale whose encoding is not UTF-8 a file flux-spec reads as written
  is no longer refused or misplaced.
- Under kernel `0.2.0` the generator computes a slice a column at a time
  (ADR 0010, step 4), so `fluxsim plan` and `fluxsim apply` on one job run about
  ten times faster than under kernel `0.1.0`: 9.0 to 9.5 s of CPU time against
  97.8 to 104.9 s on the related-star fixture scaled to 216,065 rows. A
  column's draws for every row of a slice are one Philox call over the rows'
  counters, and `flux_engine.core.kernel2` has an array twin of each draw, of
  generated identifiers and of `permute/2`, each held to its scalar function
  bit for bit. A related plan works out its keys, its foreign keys' nulls and
  picks, and the parent rows they name, for a whole slice at once. A plan with
  a column drawn `given` another, and a slice in which anything is refused,
  keep the per-row path, so a refusal is raised as before, and the rules, a
  contract's and a bundle's FLUX-side ones, still set their columns on each row
  after it is drawn. The rows are the same: no digest or golden moves, and
  `GOLDEN-CHANGELOG.md` has no new row.
  Kernel `0.1.0` is unchanged and stays the default for this release; a minor
  release of its own switches it. ADR 0010's last amendment records the
  measurement, decides step 5 (no compiled accelerator) and the default.
- `flux_stats.exact_yaml` says where YAML it cannot read goes wrong. `load_documents`
  named only PyYAML's error type (`unreadable YAML (ParserError)`), and
  `check_documents` the first line of its message, with no place. Both now name
  PyYAML's problem and its line and column: `unreadable YAML: expected ',' or
  ']', but got ':' (line 3, column 2)`. A character YAML does not allow, which
  PyYAML places by an offset rather than a mark, is placed by line and column,
  counting line breaks as YAML does. A scalar its explicit tag cannot build,
  such as `!!bool maybe`, for which PyYAML raises a `KeyError` with no place,
  is refused by its position, as `check_documents` refuses it. forge-cli's
  profiler reads a bundle through this read once it adopts it, and so keeps the
  line numbers its own reader gave.
- A bundle's files are held to two size caps before any is read (ADR 0009,
  amendment of 2026-11-18, item 6): 4 MiB for one file and 256 MiB for its
  documents and lockfile together. Every read of a bundle file took it whole,
  the validator's, the exact read and the digests, so a multi-gigabyte
  `.flux.yml` was read into memory before any bound applied.
  - `load_bundle` takes every file's size with `stat` before it opens any.
    Each read, through `flux_engine.seam.resolve.read_bundle_file`, takes the
    file's `stat` again and reads at most one byte past the cap.
  - The same read refuses a path that is not a regular file, such as a FIFO or
    a device, with no byte read. That covers `fluxsim ship`'s read of the
    bundle's contract, which `/dev/zero` behind a `.fluid.yml` link could keep
    reading without end, and `fluxsim init --contract`'s read of its contract.
  - `load_bundle`, `fluxsim ship` and `fluxsim init` refuse a file or a bundle
    past a cap with `FLX-3105`, naming the file and its size, or the total.
  - In an evidence archive, `verify_evidence` holds each document, lockfile and
    pack product (the manifest, which flux_spec reads whole) to the file cap by
    the size its header declares, before flux_spec reads it. flux_spec's own
    cap, the same 256 MiB, holds the archive's members together.

  `MAX_FILE_BYTES` and `MAX_BUNDLE_READ_BYTES` are constants, with no override.
  The largest bundle file in this repository holds 4,895 bytes.
- Every stage runs and stamps the kernel its plan names (ADR 0010, step 2).
  `plan.json` records `kernelVersion`, and `apply`, `replay`, `verify` (with
  `--update-golden` too) and `ship` now take the kernel from it rather than
  from one constant, so a plan recomputes the same `inputDigest`, and every
  digest after it, whatever kernel a new plan takes. `flux_engine.core.kernels`
  lists the kernels this engine implements, kernel `0.1.0` alone, and
  `DEFAULT_KERNEL` is the one a new plan takes. A plan that names a kernel this
  engine does not implement is refused with the new `FLX-2119`, and a
  `run-digest.json` that names a different kernel from its plan's with
  `FLX-2113`. `flux_engine.stages.pipeline` no longer has `KERNEL_VERSION`,
  `SEED_TREE_VERSION` or `TRACE_VERSION`. A new plan's versions are
  `DEFAULT_KERNEL`'s, and the kernel registry takes the seed tree and trace
  versions from `flux_engine.core.seedtree` and `flux_engine.core.trace`, which
  own them, so the release guard no longer compares copies of them.
  `pipeline.input_digest_of` and `verify.recompute_input_digest` take the
  kernel as a keyword, and the `FLX-4103` refusal names no kernel version. No
  digest or golden moves, and `GOLDEN-CHANGELOG.md` has no new row.
- ADR 0012, amended 2026-11-18, records two ways a profile says that a range
  holds no record which lie outside the publication rule: the declared levels
  missing from a `support/1` or from a grouped statistic's groups, and the
  part of the declared range beyond a published `min` or `max`. PROFILE-SPEC
  §3 already named the first and now names the second: where zeros are
  disclosive, the rule withholds a `quantile/1` whose witness is the column's
  smallest or largest value, though a published `min` or `max` already shows
  that no record lies beyond it. Nothing a profiler publishes changes.

### Fixed

- flux-stats' refusal of a document that writes out too many values through
  its aliases stopped mid-sentence: "writes out more than 1000000 values once
  its aliases are". It now ends "once its aliases and merge keys are written
  out", as `flux_stats.exact_yaml`'s docstring names the bound.
- `fluxsim ship` and `verify_evidence` hold a bundle's documents to the exact
  read's bounds before flux_spec's validator reads them. Both handed a bundle to
  flux_spec's own read, a plain SafeLoader that bounds nothing, so a document of
  a few hundred bytes made of aliases stood for millions of values, which the
  schema check then walked. The seam already held its own reads to the bounds,
  and these two did not; an archive verified from anywhere is the likelier
  case. `pack_evidence` checks the staged tree it packs, so nothing can change
  between the check and the pack. `verify_evidence` reads the archive's
  documents and lockfile once, front to back, within flux_spec's own member and
  byte caps, decoded as the validator will decode them, and refuses one past the
  bounds with `FLX-3105` before flux_spec reads any. It takes a member for the
  lockfile under any name a case-insensitive filesystem opens for `flux.lock`,
  such as `FLUX.LOCK` on macOS. Both check the lockfile whenever it is there,
  though flux_spec reads it only for a versioned ref, so a lockfile past the
  bounds, or one that does not decode, is refused even then. Otherwise a bundle
  within the bounds packs and verifies as before.
- A bundle's YAML is read within bounds:
  - a document may write out at most 1,000,000 values and 64 MiB of characters
    once its aliases and merge keys are written out, and nest at most 64
    levels;
  - the merge keys of one document may name at most 1,000,000 mappings;
  - a number may need at most 1,000 digits written out, a zero counting the digits its scale spells;
  - the merge keys of one bundle read may copy at most 100,000 pairs.
  A string is one value however long it is, so its characters are counted too:
  2,000 aliases of one 50,000 character string, 58 KB of text, write out 100
  MB. So is a number, which is spelled in full: it counts as the digits its
  decimal is spelled in when that is more than its text, so `1.e+999` is a
  thousand characters, and about 320 bytes of aliases of it write out 200 MB. A merge
  key that names empty mappings writes out nothing, yet building the document
  visits each of them for every mapping that holds the key, so they are counted
  too: an empty mapping, a list of 4,000 aliases of it and 4,000 mappings that
  each merge the list are 56 KB, 8,007 values and 16 million visits. What a
  merge key names is worked out once however many mappings hold it. The measure
  walks a document with a stack of generators, as PyYAML's constructor does, so
  how deep a document is does not use the interpreter's stack.
  Past a bound the read refuses with `FLX-3105`, naming the file and the line.
  So does a document that holds itself through an alias, and one nested too
  deeply to parse. No released FLUX reference validator bounds its read. Its main
  branch has held a document to the same four bounds, on values, levels,
  mappings named and characters, since 2026-11-19 (Agenticstiger/flux#37), and
  counts a number as its text, so it accepts what only a number spelled in full
  makes too long. Every file is still checked against these bounds first,
  decoded as the validator decodes it. Before, a kilobyte of aliases, or a few
  hundred bytes of merge keys, could take minutes and gigabytes, and a deep
  document ended the run with a `RecursionError`.
  The canonical form keeps the scale a number was written with, so a zero
  counts the digits its scale spells: `0.e-10000000` was read, and then spelled
  in 10,000,002 characters, and is refused now as a zero of a scale of ten
  million digits, as `1.0e-10000000` is. The count is the one the fidelity
  block's readers and the parameter register share, so a quoted `"0e-10000000"`
  there, which was spelled in full, is refused too (`FLX-5030`).
- A bundle file that is not text in the locale's encoding, or that holds a
  character YAML does not allow, is refused with `FLX-3105` before the
  validator reads it. The validator meets either only where the file holds
  it, after building every document before it, and a file that did not
  decode ended the run with a `UnicodeDecodeError`. A path that is not a
  regular file, such as a FIFO or a device, is refused without being read.
- A scalar in a bundle file that its explicit tag cannot build
  (`!!bool maybe`, `!!float ZZZ`) is refused with `FLX-3105` by its position,
  where the validator's read raised a bare `KeyError` or `ValueError`.
- `fluxsim init --contract` reads the contract within the same bounds. A
  contract nested too deeply to parse, or with a scalar its explicit tag
  cannot build (`!!bool maybe`), is refused with `FLX-3105` instead of a bare
  Python error.
- A FLUID column declared `decimal(p,s)` with leading zeros in p or s, such as
  `numeric(000012, 00002)`, had no FLUID SQL/1 type once a part ran past four
  characters, because the four-digit cap counted the zeros, so every text
  naming it was `FLX-6111`. p and s are now read by value, as the SQL standard
  reads an unsigned integer and forge-cli's reader does: that column is
  `numeric(12, 2)`. The bounds are unchanged, a type only when
  1 <= p <= 1,000 and 0 <= s <= p. The shared vectors, re-vendored from
  forge-cli, gain 13 cases at those bounds, from `decimal(1)` and
  `decimal(1000, 1000)` to `decimal(0)` and `decimal(1000,1001)`. No digest or
  golden moves.

## 0.6.2 - 2026-11-18

Related tables are generated together, `fluxsim init --contract --profile`
writes a bundle from a FLUID contract and a profile, and a FLUID 0.7.7 enum
level reads as written. The engine now needs flux-spec 0.5.3.

Two bundles that the FLUX reference validator accepts and this engine cannot
run are now refused with an `FLX` code at `fluxsim validate`, instead of
stopping `plan` with a Python `KeyError`. The FLUX specification's quickstart
bundle declares neither key. A bundle that ran before plans to the same bytes,
and no digest or golden moves.

### Added

- `fluxsim init <dir> --contract <file> --profile <file>` writes a bundle for
  your own data: the FLUID contract, copied unchanged, the least World and
  Persona a run needs, and a Simulation with one generator emit per expose the
  profile describes. It reads the two documents and no record. The Simulation
  pins the profile's digest, and its parameter register labels every number it
  declares: a published aggregate of the profile (cited by a JSON Pointer), a
  fact of the contract, an assumption with its reason, or a template default.
  A statistic of the profile is used first, then the column metrics, then the
  contract; a column's `range` and `support`, which the fidelity check reads,
  come only from published values or the contract. A count withheld as small
  (status `A`) is assumed to be half the disclosure rule's minimum cell, a
  cell withheld under any other status the mean of the published cells, and a
  withheld null count no nulls. A published mean shapes the histogram on the
  grid the generator draws on (each bin keeps its share of the rows), a
  histogram's draw keeps to the published `min` and `max`, and a `mean/1` over
  a number column whose range is published becomes a `mean` property, so a run
  made from a profile `fluid profile` writes can pass `verify --profile` and
  ship. A boolean or a date column carries no property, since the check cannot
  read one against a profile. Keys come from the contract and are never
  restated: a number key lies between its published `min` and `max`, or over
  an assumed span that covers the foreign keys that reference it and lies
  inside the key it references, down a chain of keys, and a foreign key the
  profile says nothing of is drawn over the span of the key it references. A
  column of a key over several columns spans its rows too, and a composite
  foreign key takes its nulls from its first column. An integer column whose
  published values pass 2^53, which a run's rows cannot hold exactly, is a
  labelled placeholder. The bundle is checked as `validate` and `plan` check
  it from the documents alone before a file is written, and nothing either
  refuses is written, such as a foreign key into a table the command leaves
  out (`FLX-3403`). See `docs/how-to/write-a-bundle-from-a-profile.md`.
- Parameter register format 1-1-0 adds the `contract` source: a number the
  FLUID contract fixes, such as the number of levels an `enum(...)` type
  declares, cites the contract by its `productRef` and a JSON Pointer into it.
  `validate` and every other command refuse a citation that names no contract
  the run emits from, or reaches nothing in it (`FLX-3105`). A register with
  no contract entry keeps format 1-0-0, so no existing record, plan or digest
  moves. The plan, verify and ship lines and the evidence report give the
  contract's share when there is one. The 1-1-0 schema is
  `docs/spec/parameter-register-1-1.schema.json`.
- Related tables are generated together. For the exposes a Simulation generates
  from statistics, every key a FLUID 0.7.7 contract declares is unique, and every
  foreign key value is a key its parent's rows hold, with nothing held in memory,
  so `--jobs` gives the same bytes. A one-column number or date key is laid out in
  ascending order over its declared range or bins; a text key, and a key of
  several columns, is a keyed permutation of the row index, so it never repeats at
  any length. A foreign key that declares a draw keeps its shape and takes the
  parent's nearest key; one that declares none picks its parent uniformly; a
  `one_to_one` relationship references each parent row once. An optional foreign
  key keeps its `nullFraction` when the relationship's `joinType` is `left`. What
  cannot hold is refused before any row exists (`FLX-3403`). Contracts without
  keys, and every existing golden, are unchanged. KERNEL-SPEC sections 4.1 and 8.2,
  a vector file for the permutation, and a new golden set,
  `tests/golden/related-star`.

- flux-stats: `flux_stats.fidelity`, the fidelity-block grammar. It parses
  the block a Simulation declares (`parse_fidelity_block`) against the expose
  it names, refuses what SEAM-FIDELITY-SPEC section 2 refuses with `FLX-5030`
  to `FLX-5034`, and reads a `support: declared` column's levels from its
  `enum(...)` type (`declared_enum_values`, `FLX-3001`). It also holds
  `measure_group`, which forms one group of a property from the parsed
  declaration, so a profiler forms each group with the check's own function.
  The module moved from the engine: every engine name is an import of the
  same object, the codes keep their titles and explanations, and the engine's
  behaviour is unchanged.

### Changed

- The engine needs flux-spec 0.5.3 or later (`flux-spec>=0.5.3,<0.6`). Its
  FLUID 0.7.7 preview, re-pinned to forge-cli `e88301f7`, accepts every enum
  level the engine reads: a quoted level may hold any character but a control
  character, its own quote doubled. `docs/reference/schemas.md` now describes
  FLUX 0.5.3.

### Fixed

- A World with no `spec.vertical`, or one naming a pack this engine does not
  ship, passed `fluxsim validate`, and `fluxsim plan`, `run` and `expr` then
  stopped with a `KeyError` (`unknown vertical ''` for a World with none) and
  the generic "run again with FLUX_DEBUG=1" hint, even when a fidelity block
  declared every column.
  `validate` now refuses it with the new `FLX-5042`, and `plan`, `run` and
  `expr` give the same refusal. The message names the World, the missing key
  and the packs this engine ships, for example `vertical: telco`.
- A Simulation with no `spec.timeBounds` passed `fluxsim validate`, and
  `fluxsim plan` and `run` then stopped with `KeyError: 'start'`. `validate`
  now refuses it with the new `FLX-5023`, and `plan` and `run` give the same
  refusal, before the population is built. The message names the Simulation
  and the missing key, with an example window.
- The README, the architecture page and the how-to "Score a run against a
  profile" said that `fluid profile` writes no statistics, so a run checked
  against its profile is always refused. From data-product-forge 0.31.0 it
  writes an ungrouped `mean/1` for each number column, unless the output checks
  withhold it, and a `mean` property over such a column is scored. The pages
  now say so, and which properties such a profile cannot answer.
- A FLUID 0.7.7 enum level reads as written. A quoted level may hold any
  character but a control character, a comma, a parenthesis, a dot and `>`
  among them, and a doubled quote inside it now reads as one quote (`'O''Brien'`
  is `O'Brien`, where it used to read `O''Brien`). A level holding a comma used
  to be cut in two, and one holding a parenthesis read as no levels or made the
  engine refuse the contract. A list the grammar refuses, such as
  `enum('a' 'b')`, is refused with `FLX-3001`, and so is an unquoted level that
  is not a word list of ASCII letters, digits, `_` and `-`, such as `enum(1.5)` or
  `enum(a.b)`, which used to read as written: quote it, as in `enum('1.5')`.
  `flux_stats.fidelity.split_type_params` is new and splits a column type the
  way forge-cli does.
- A FLUID SQL/1 text can name an enum column whose quoted level holds a
  parenthesis, such as one typed `enum('a (b)', 'c')`. The column-type table
  read a type up to its first `)`, so the column had no FLUID SQL/1 type and
  every text naming it was `FLX-6111`. It now cuts the type with
  `split_type_params`, as the seam and forge-cli's FLUID SQL/1 reader do, and
  only a type whose parameter list holds a parenthesis inside quotes reads
  differently. The shared vectors, re-vendored from forge-cli, gain three cases:
  a level holding a parenthesis, one holding a comma and a doubled quote, and a
  mistyped level that is still refused. No digest or golden moves.

## 0.6.1 - 2026-11-17

A documentation release: nothing the engine writes changes.

### Added

- An architecture page (`docs/explanation/architecture.md`), linked from the
  README and both documentation indexes: what crosses over from the data, the
  parts you install, the packages inside the engine and the import rules
  between them, the life of a run, the design patterns behind it, and how
  quality is kept. Its diagrams are Mermaid flowcharts. The documentation
  harness now accepts a `mermaid` block and holds it to a flowchart subset
  that GitHub and the site both draw, and a test checks that every repository
  path a page names exists.
- Documentation: how-tos for scoring a run against a profile, in full or
  sampled, and for sealing and scoring a prediction; indexes for the
  specifications and the decision records; a tutorial that reads its run id
  instead of printing a fixed one; and a plain statement that a profile from
  `fluid profile` cannot score a run yet, because it carries metrics but no
  statistics (`FLX-5037`).

## 0.6.0 - 2026-11-17

Every column of every exposed table now has a recorded producer, the columns
that have none are reported, and the plan holds a digest of that record. The
digest is a new key in `plan.json`, so it moves the `planDigest` of every
bundle, and with it every run id, the `lineage` ledger and every `runDigest`
(`GOLDEN-CHANGELOG.md` has two new rows for it). The rows, every component
digest, `run-digest.json` once its plan digest is masked and every seam
attestation are the bytes they were. `KERNEL_VERSION`, `SEED_TREE_VERSION` and
`TRACE_VERSION` keep their values.

Upgrading moves every run digest once, so a golden baseline recorded with
`fluxsim verify --update-golden` holds the old value, and so does a
`spec.golden.baselineDigest` a bundle declares. The first `verify` then reads
the golden leg as moved while every component reproduces. Check that no other
leg failed and record the baseline again with `--update-golden --reason
"<why>"`.

### Added

- Sample-based verification (`docs/spec/SAMPLE-SPEC.md`). `fluxsim verify
  --profile PATH --sample FRACTION` compares each emit's means with the
  pinned profile on a sample of whole blocks of records (`--sample-block`,
  1000 by default) chosen by a seeded hash of the run, the emit and the
  block (`--sample-seed`), and decodes only those blocks for the
  comparison; the digests and the rows' conformance still read every
  record. Each `mean/1` group is estimated with the one-stage cluster ratio
  estimator and carries its sample size and a 95 % interval, exact and
  widened only by rounding a t table up and a square root up
  (`flux_stats.sampling`). The interval decides: `within` inside the
  property's band, `outside` wholly beyond it, and `undecided` across an
  edge, which fails the check as `outside` does. The full comparison's
  floors and refusals hold on the sample, and a refusal names a row by its
  place in the emit (`flux_stats.reading`'s `group_keys` and `read_column`
  take a `first_row`). The other kinds are named as not estimated, a
  sampled verify forms no scorecard, and `ship` still reads every record. A
  five per cent sample covers the full mean at least ninety times in a
  hundred (the tests).
- Keys from the FLUID contract (KERNEL-SPEC section 8.1, `flux_engine.seam.keys`). A contract on FLUID 0.7.7
  declares its primary and foreign keys as `exposes[].semantics.relationships` (a composite key is one relationship)
  and as primary, unique and natural entities, and the engine reads them instead of a FLUX declaration. A
  relationship's `toColumns` are a key of its target; in a generator-driven emit a one-column STRING key is
  generated as an identifier with no fidelity `unique` and no tag. Three codes are new: `FLX-3005` refuses a key the contract declares that does not
  resolve, `FLX-3111` refuses a Simulation that restates a key (a key block in its extensions, or a fidelity
  `unique` that contradicts the contract), and `FLX-3112` refuses an emit of a keyed contract that pins no
  `provenance.contractDigest`, so a moved key stops the seam. A contract on FLUID 0.7.6 or earlier is read as
  before, and no golden moved; a key-like entry in a Simulation's extensions, refused before as `FLX-3105`, is
  now `FLX-3111` whatever the contract's version. A keyed contract cannot carry a YAML float, or text not in Unicode
  NFC, yet: the reference validator and the engine canonicalise those differently, so no one pin satisfies both. Until a flux-spec release carries FLUID 0.7.7, the engine builds against
  Agenticstiger/flux#23 through a uv git source.
- A parameter register (docs/spec/PARAMETER-REGISTER-SPEC.md). A Simulation can declare, under the new
  top-level `parameters` key of its `com.agentics.flux/1` namespace, what each number its run reads rests on: a
  statistic of the profile it pins (a JSON Pointer into the profile and the statistic's registered definition), an
  assumption with its reason, or a default. Entries name numbers by document id and RFC 6901 pointer, a pointer to a
  mapping or a list names every number under it, and the most specific entry decides. The register counts every JSON
  number under `spec` in the run's cast and in the Playbacks bound to its World, and the numbers the namespace's
  fidelity block and scorecard thresholds declare; seeds, the sweep and the fidelity block's `encoding` and `budget`
  are not counted, and a number no entry names is a default. `apply` writes `run/parameter-register.json`, the plan
  holds its digest as `parameterRegisterDigest`, `verify` and `ship` read it only through that digest, `ship` packs
  the bytes it checked, and `plan`, `verify` and `ship` report the shares on a `parameters:` line. `EVIDENCE.md`
  states them under "What Was Not Proven" and lists every assumption with its reason. `plan` refuses a citation the
  pinned profile does not publish (`FLX-3113`), and a record that is missing, moved or not bound by the plan is
  `FLX-2116`. A Simulation that declares no register plans to the same bytes as before: no golden, digest or
  report moves.
- Shape flags (`docs/spec/SCORECARD-SPEC.md` §6). `fluxsim verify`
  and `fluxsim ship` read every emit whose table the pinned profile
  describes, column by column, whether or not a fidelity property is declared
  on it, and flag what the profile makes unlikely: `EMPTY_BINS`, cells the
  profile publishes a count for that the run leaves empty, when the exact
  chance of an empty cell, `(1 - p)^n`, is at most the test level, a record
  outside every cell counting against every cell; `IMBALANCE`, each value
  holding at least a tenth of a column's records that the profile's share of
  its cell cannot explain (Chernoff's bound on the binomial tail); `CONSTANT`,
  one distinct value where the profile makes that too unlikely; and
  `HIGH_CARDINALITY` or `LOW_CARDINALITY`, a distinct count above twice the
  most, or below half the fewest, distinct values the profile allows a run of
  that size. The names are ydata-profiling's where one fits. The level is `1/1000` per run, split
  across every test (Bonferroni); a count a disclosure rule rounded is read as
  the share least favourable to a flag, and a withheld cell is never tested.
  The document carries the run's counts and the profile's cells by ordinal,
  and no count of the profile.
  The flags inform: they are in the scorecard's `shapeFlags`, `verify`'s lines
  and `EVIDENCE.md`, and withhold nothing. The scorecard format is `1-1-0`,
  which adds `shapeFlags`' status `checked` and changes nothing format `1-0-0`
  says, so a pinned run's `scorecard.json` changes once. A run that pins no
  profile is unchanged, and no golden moves.
- `flux_stats.tails` (flux-stats): exact decisions about binomial tails
  without writing them out, from binary bounds rounded outwards and, where
  those cannot tell, the exact `Fraction`.
- flux-expr/2, the language of the rules about valid data (EXPR-SPEC-2, ADR 0008), as `flux_engine.expr2`. Its core is
  FLUID SQL/1 (forge-cli's `RFC-data-rules.md` §2), the text of a contract's invariants, and a FLUX-side text may also
  use the previous version of an entity, paths over several links, aggregates over a parent's children and windows
  over a row's siblings. The package parses a text (no parser library: the core gains no dependency), checks its names
  and types against the expose's row scope, gives its static scale and what it reads, writes its canonical text, and
  evaluates it exactly: numbers are `Fraction`s, `x / 0` is NULL, truth is two-valued, `ROUND` rounds ties away from
  zero or to the even neighbor, and `DAYS_BETWEEN`, `MONTHS_BETWEEN` and `YEARS_BETWEEN` count whole units. Sixteen
  codes are new: `FLX-6101` to `FLX-6113`, `FLX-6201`, `FLX-6206` and `FLX-6209`. A text is at most 4,096 code points
  and 64 levels deep, where every operator, call, `CASE` and parenthesis is a level, and `ROUND`'s scale is at most 38.
  The core passes every vector of forge-cli's `fluid-sql-vectors.json` (251, vendored unchanged as
  `tests/vectors/fluid-sql-vectors.json`), and reads FLUID SQL/1 as that file does where the RFC leaves a reading open:
  a NULL literal takes its type only from a typed operand beside it, `timestamp with time zone` is `timestamptz`, and a
  string column's placeholders are exempt from its enum. `tests/vectors/expr2-core-vectors.json` (175 vectors, in the
  same shape) and `expr2-vectors.json` (77) pin further trees, types, canonical texts and values, and DuckDB checks
  every aggregate, window and date difference among the three files in the test suite, except `AVG` and division,
  which DuckDB computes in `DOUBLE` and the vectors pin as exact rationals. flux-expr/1 is unchanged, and nothing
  reads flux-expr/2 yet, so no bundle, digest or golden moves.
- Column coverage (SEAM-FIDELITY-SPEC §1). A column is `declared` (its entry
  under `fidelity.columns`), `copied` (a runtime attribute, named by the emit's
  `mapping` block, the vertical's alias table or the column's own name),
  `derived` (an identifier, from the seam digest and the row index) or
  `default`. `profile` is in the set for later and nothing produces it yet. A
  `default` is an optional column nothing maps onto, which renders `null`, or a
  DATE or TIMESTAMP column that declares no draw of its own, which is drawn
  uniformly within the simulation's time bounds. `fluxsim plan` names every
  such column and goes on.
- `--strict-coverage` on `plan`, `apply` and `run` (`strict_coverage` in the
  library) refuses a run in which a column fell back to a type default. The
  new `FLX-3304` names every such column of every emit in one table, before any
  row is drawn, the shape dbt gives a failed enforced model contract, and says
  how to cover a column of each kind of emit: a mapping for a scenario-driven
  one, a range, a rate, or bins with `binWeights` under `fidelity.columns` for
  a generator-driven one. The setting changes whether a run is accepted and
  never what an accepted run produces. A Simulation can declare it as well
  (the `strictCoverage` entry below).
- `apply` writes `column-provenance.json` into the run directory: per expose,
  each column's origin, its source and who chose it, with an OpenLineage
  `ColumnLineageDatasetFacet` (1-2-0) that gives each copied column its source
  attribute as the input field. `flux_engine.lineage.emitter` builds the facet
  with no OpenLineage import, and `tests/lineage` reads it with the client's own
  classes when the `lineage` extra is installed. `apply` writes it through a
  staged rename, so a link planted at its name is replaced and not written
  through.
- `plan.json` records `columnProvenanceDigest`, the `raw-bytes` digest of that
  file, the way it records `seedTreeDigest` for `seed-paths.ndjson`. Any change
  to the file moves it, reformatting and a repeated key included. `ship` and
  `verify` read the record only through it: a record that is missing, is not a
  regular file, no longer hashes to the plan's digest, or is not one this build
  can read ends the command with the new `FLX-2115`, and a ship packs nothing.
  The pack carries the bytes `ship` checked, not whatever the file holds by the
  time the directory is copied. `apply` makes the same comparison on the record
  it is about to write and refuses before it writes a row when the plan names a
  different one. A record edited on its own, for a generator-driven emit as for
  a scenario-driven one, can no longer reach a pack. Both `ship` and `verify`
  also refuse a `run-digest.json` that names a different plan from the
  `plan.json` beside it (`FLX-2113`), the link between the plan's digest and the
  run digest the runtime statement names. A plan written before this carries no
  digest and binds no record, so `ship` says that no column provenance is bound
  to the run, says to plan and apply again, and believes nothing of a file it
  cannot check.
- `EVIDENCE.md` has a sixth section, "Column Provenance", before "What Was Not
  Proven", and that last section names the columns that fell back to a type
  default, or says none did, or says that no record is bound to the run. The
  sentence for each column comes from a fixed table keyed by origin and
  producer, and a row whose origin, producer and source do not fit together is
  refused, so the file supplies no wording. The section says that the plan holds
  a digest of the record and that `ship` checked the record against it, and that
  `ship` does not work out again how a column was produced.
- The design of flux-expr/2 (`docs/spec/EXPR-SPEC-2.md`, ADR 0008). Its core
  is FLUID SQL/1, the SQL expression subset that the FLUID RFC
  `RFC-data-rules.md` defines for the rules about valid data a contract
  declares: invariants with a band of violating rows, dependencies and shared
  attributes, which FLUX reads, never restates, and uses both to generate the
  rows and to check them. On top of the core, FLUX adds the previous version,
  paths over several links, aggregates over a parent's children and SQL
  windows over a row's siblings, for the rules only a generator needs.
  flux-expr/1 is unchanged and stays a language of its own. Nothing is
  implemented yet, and no code changed.
- The profile document (`docs/spec/PROFILE-SPEC.md`, with its JSON Schema
  `docs/spec/profile-1.schema.json` and an example). A profile carries
  aggregate statistics of the data a bundle imitates, computed where the data
  lives: per column, per column pair, per relationship (children per parent),
  per period (counts and sums per month) and per query (rates). Every
  statistic names its registered definition, a sample-size band and the
  disclosure rule applied to it, and a group or cell the rule withheld carries
  its SDMX `CL_CONF_STATUS` code and no value. Counts are integers and every
  other number is a decimal string. The envelope (`_producer`, `_schemaURL`)
  and the column metric names (`count`, `nullCount`, `distinctCount`, `sum`,
  `min`, `max`, `quantiles`) are OpenLineage's, and the bin layout is
  mostlyai-qa's. `flux_stats.profile` reads a profile whole or refuses
  it with the new `FLX-5036`. The format grows by minor versions that only
  add, and the spec says how the planned additions fit that rule: uncertainty
  inputs, statistics by period and as series, query-result cells, and the
  share of rows that satisfy each declared invariant.
- A Simulation pins a profile by its digest, under the new top-level
  `profile` key of the `com.agentics.flux/1` namespace. `fluxsim plan
  --profile PATH`, and `run` and `apply` when they take a fresh plan, refuse a
  pin with no profile, or a profile with no pin, with the new `FLX-3109`, and
  a profile whose digest is not the pinned one with the new `FLX-3110`, before
  anything is written. `apply --run-id` refuses `--profile`: the recorded plan
  was paired with its profile when it was taken. A bundle that pins no profile
  plans exactly as before.
- The fidelity property kinds can read their expected statistics from a
  profile instead of a committed slice, compared by the same registered
  definitions (`flux_engine.concordance.verifier.ClearProfile`). A group or a
  count-table cell the profile withheld is censored, never scored as zero: a
  count table is compared over the cells it published, each side renormalised
  to them. A profile that holds no statistic of a property's shape, or holds
  one that depends on the grid (a sum, a moment, an order statistic, a bin
  count) formed on another grid, is refused with the new `FLX-5037`. A
  profile formed over a committed slice gives the slice path's outcomes on
  every vectors case. The record of SEAM-FIDELITY-SPEC §6 is still written for
  committed states only.
- A Simulation can declare strict coverage itself: `strictCoverage: true` at
  the top level of its `com.agentics.flux/1` extensions refuses a run in which a
  column fell back to a type default, with the same `FLX-3304` and table as
  `--strict-coverage`, before any row is drawn. The run is strict when the flag
  or the declaration asks, and the flag cannot turn the declaration off: it is
  part of the bundle `inputDigest` covers. The refusal names which one asked.
  The value is a boolean (anything else is `FLX-3105`, `validate` included);
  `false` and a null declare nothing. The key is a bundle edit, so it moves
  `inputDigest`, but `plan.json` has the same keys with it as without, and a run
  it accepts makes the rows, the column provenance record and every component
  digest it makes without it. No bundle in the repository declares it, so no
  golden moves.
- The scorecard (`docs/spec/SCORECARD-SPEC.md`, with its JSON Schema
  `docs/spec/scorecard-1.schema.json` and an example). `fluxsim verify` and
  `fluxsim ship` take `--profile PATH`, pair it with the pin the run's plan
  records (`FLX-3109`, `FLX-3110`, `FLX-5036`), compare every emit that
  declares a fidelity block with the profile, and score the run per property,
  per table, per fidelity dimension and overall. A property scores the exact
  similarity of its scored groups to the profile, SDMetrics' measures formed
  from the comparison's own integer quantities: one minus the total variation
  for a count table, the share of records on the support for a support, one
  minus the difference over the declared range for a mean and a quantile, and
  an exact lower bound of one minus half the difference for a correlation. A
  table and a dimension score the mean of their scored properties, and the run
  the mean of its measured dimensions. A censored or refused property is shown
  and left out of every mean, and a score that was never formed is `null`,
  never a default number. The eight dimensions are `structure`, `marginals`,
  `joint`, `crossTable`, `temporal`, `invariants`, `queries` and `defects`;
  this release scores the first three. Each table carries an OpenLineage
  `DataQualityAssertionsDatasetFacet` (1-1-0) with one assertion per compared
  property. Groups are named by ordinal, and no value of the data appears in
  the document. `ship` withholds an emit whose comparison with the profile is
  not within, packs `run/scorecard.json` when the run ships, and summarises it
  in `EVIDENCE.md`. `fluxsim run --profile` hands the profile to `--verify`
  and `--ship`. The slots for the shape flags and the sample-based intervals say
  `not checked` and `not computed`.
- Thresholds on the scorecard, under the new top-level `scorecard` key of the
  `com.agentics.flux/1` namespace: a floor on the overall score, on a
  dimension or on a table, written with ODCS's `mustBeGreaterOrEqualTo` or
  `mustBeGreaterThan`. A missed threshold withholds every emit whose
  properties feed the score, with a check that names the lowest-scoring
  property. An emit's `minCredibility` is read against its table's computed
  score wherever the table was measured, which replaces the Playback's
  declared credibility as its basis. A threshold that could only withhold (in
  a Simulation that pins no profile, on the overall score, a dimension or a
  table no declared property is scored in, or `mustBeGreaterThan 1`) is
  refused with `FLX-3105` by every command, before anything is written.

### Security

- The files `plan`, `apply`, `ship` and `verify` write under the state directory no longer follow a link. They
  go through `flux_engine.safeio`: a file written whole is staged beside its name and renamed over it, so a link
  planted at the name is replaced and its target is not touched; the lineage ledger is appended to through an
  open that refuses a link, a directory or a pipe (`FLX-2117`); and the files `ship` and `verify` read back to
  digest or report on are opened without following a link. `RunLayout.ensure` refuses a `runs/`, run or
  `emits/` directory that is itself a link, and a run directory that already holds a link, and `verify
  --update-golden` does the same for `golden/` (`FLX-2118`). A link at the name of a file `apply` writes was
  replaced before and is refused now, with nothing written. The same rule covers the boundary vault
  (`<state>/boundary/`: its directory, its lock and its store), the four outputs of an evidence pack (packed in
  a private directory and moved into the output directory by a staged rename, which must not itself be a link)
  and the file sinks' stream directory and manifest. A file is written with the mode the umask leaves, and its
  directory is synced after the rename. A `.<name>-XXXX.tmp` file a killed write left behind is not packed. A
  read that meets a link is `FLX-2117`, which is also an `OSError`. What is left open (checks that run before
  the use, the Windows fallback) is recorded in the `flux_engine.safeio` docstring. No output byte moves: no
  golden, digest or pinned file changes.

### Changed

- **flux-stats, a second distribution.** The statistic registry, the column
  metrics, the profile document reader, the fixed-point grid and the
  `FLX-nnnn` catalog are now the `flux-stats` distribution (import
  `flux_stats`), built from `packages/flux-stats` in this repository and
  released from the same tag at the same version (ADR 0009). It depends on
  the Python standard library alone, runs on Python 3.10 or newer (one release
  below the engine, for forge-cli's profiler) and imports nothing of the engine,
  so a profiler can form the fidelity statistics where the data lives without
  installing the engine. flux-sim-engine depends on exactly the flux-stats of
  its own release (`flux-stats==X.Y.Z`), so `pip install flux-sim-engine`
  installs both and the engine's core gains no third-party dependency.
  `flux_engine.core.fixedpoint` is now `flux_stats.fixedpoint`, with no alias.
  `flux_engine.core.errors` stays and re-exports the same catalog and the same
  `FluxError` class, so a sink plugin's imports keep working. Every FLX code
  and message, every digest and golden, and `KERNEL_VERSION`,
  `SEED_TREE_VERSION` and `TRACE_VERSION` are unchanged.
- **The record reader is flux-stats's too** (ADR 0009, amended 2026-11-17).
  `flux_stats.reading` reads a record the way the fidelity check does: a
  number as the exact rational it spells, an RFC 3339 instant as integer
  milliseconds, a category as its NFC string, each column's null policy, the
  groups, and the refusals `FLX-5005`, `FLX-5022` and `FLX-5031`. A profiler
  calls it with its own column objects, and the check calls it with the
  engine's. `FLX-5022` and `FLX-5031` are registered there now, with the same
  titles and explanations, and the engine's names for the moved functions and
  enums (`flux_engine.core.clock.parse_rfc3339_ms`,
  `flux_engine.core.canonical.nfc`, `NullPolicy` and `ValueDomain` in
  `flux_engine.concordance.predicate`, and the boundary's `encode_value` and
  `level_cells`) are the same objects. Every digest and golden is
  byte-identical.
- `parse_rfc3339_ms` no longer calls `datetime.fromisoformat`, whose accepted
  forms differ between Python versions, so an instant reads the same on every
  Python: on 3.11 to 3.13 every value and every refusal is the one it was, and
  3.14 no longer accepts hour 24. A date-time whose instant falls outside the
  years 1 to 9999 once moved to UTC, such as `0001-01-01T00:00:00+00:01`, is
  refused with `FLX-5022` where it used to escape as an `OverflowError`.
- `plan.json` records `profileDigest`, and any scorecard thresholds as
  `scorecard`, when the Simulation pins a profile, so `verify` and `ship` read
  both through the plan's digest. A pinned bundle's `planDigest`, run id and
  `runDigest` therefore move once against a plan taken before this change; a
  plan whose Simulation pins none keeps its bytes and its `planDigest`, and no
  golden moves (no golden bundle pins a profile).
- `ship`'s withheld summary no longer lists a censored property as a failure.
- `fluxsim plan` plans every emit before it draws the rows of any of them, so
  a refusal in a later emit no longer waits for the earlier emits' rows.
- The `com.agentics.flux/1` namespace is closed: a key other than `emits`,
  `profile`, `scorecard` and `strictCoverage` at its top level is refused with
  `FLX-3105`, where it was ignored.
- `fluxsim verify` refuses a run directory that has no plan (`FLX-2107`), as
  `ship` and `apply` do. Without a plan it used to skip the checks that run
  through one, which now include the record's digest. Every `apply` reads a plan
  before it writes anything, so a run an engine wrote always has one.
- ADR 0003 is amended (2026-11-16). The engine never reads a record of the
  data a bundle imitates: a separate profiler package reads records only
  where they live and emits only aggregates. Its disclosure defaults take
  their values from ACRO: cells with fewer than 10 records suppressed, zero
  cells disclosive, counts rounded to a multiple of 5, and no noise by
  default. These are output-checking conventions only; no privacy or
  anonymity guarantee is made or implied. A fidelity check's result states a
  statistical relation and nothing more. Today `--committed-slice` is the one
  path that reads records, and it applies none of these defaults; it moves
  into the profiler package when that package ships. No code changed.

- One definition per statistic. `flux_stats` is a registry that maps a
  stable identifier (`mean/1`, `marginalHistogram/1`, `levelHistogram/1`,
  `support/1`, `correlation/1`, `quantile/1`) to the quantities the statistic
  keeps, the pure function that forms them, their spelling in `S` and the
  comparison SEAM-FIDELITY-SPEC §5 gives them. The fidelity check's statistic
  and verifier now go through it, and the profiler that comes next will share
  it: it imports the standard library alone, and a test pins that. Each identifier and each property kind
  has exactly one definition. The pattern is Deequ's single `Analyzer`, which
  feeds both its profiles and its checks. Nothing a run computes changed:
  every digest and golden is byte-identical.
- flux-sim-engine no longer depends on pydantic. No module imported it, so
  `pip install flux-sim-engine` stops installing pydantic and the three
  packages only it needed: pydantic-core, annotated-types and
  typing-inspection.
- The vendored copy of `flux_spec` under `flux_engine/_vendor/` is gone
  (ADR 0005, amended 2026-11-17). The `flux-spec` distribution, already a
  declared dependency, is the only source of the document validator, the
  RFC-04 enforcement decision, the evidence bundler and the schemas they read.
  An environment without it now fails with a `ModuleNotFoundError` that names
  the install command, where the engine used to run on its copy. `fluxsim
  describe --self` reports `spec_source: flux_spec` and the installed version
  as before. `flux_engine.describe.VENDORED_SPEC_VERSION` and its older
  spelling `SPEC_VERSION` are removed. `docs/reference/schemas.md` is
  generated from the installed distribution, chosen by the floor of the
  `flux-spec` requirement, so it reads the same wherever it is built. Every
  digest and golden is byte-identical.

### Fixed

- A record's number can no longer stall the fidelity check or a profiler. A
  cell such as `"1E+99999999"`, eleven characters naming a number of a hundred
  million digits, was formed as an exact rational before anything bounded it,
  which took minutes and gigabytes. `flux_stats.reading.exact` now refuses a
  number that needs more than 1000 digits written out (`FLX-5005`, value class
  `over-long number`, with no count in the message) before forming it. The
  count is the one a fidelity block's numbers are bounded by (SEAM-FIDELITY-SPEC
  §2: no exponent, a leading `0.` not counted), now
  `flux_stats.reading.digits_written_out`, which the engine's declaration
  reader imports, so one rule bounds every number read. A cell of at most 1000
  digits reads as before, and every digest and golden is unchanged.
- The outcome-accuracy store keeps the seal it writes. A second `fluxsim
  accuracy register` of a prediction id replaced the sealed record and exited
  0, so a claim could be swapped after its outcome was known. A record is now
  written once: registering the same claim again (a document that seals to the
  same digest) succeeds and writes nothing, and the `--json` result says so
  with `created: false`; a different claim under a registered id is refused
  with the new `FLX-7007`, which names the record and both digests.
- `fluxsim accuracy score` and `fluxsim accuracy record` used a sealed
  prediction without checking it against its `sealDigest`, so a record edited
  by hand was scored, and published with a digest recomputed from the edit.
  Every read now recomputes the seal from the record's fields and refuses a
  record that does not match it, is not a sealed PredictionRecord, or is the
  record of another id than its file's name, with the new `FLX-7008`. `record`
  checks every record in the store, not only those registered at
  `--registered-at`, because an edited `registered_at` would move a record out
  of its registration, and it names every record that fails in one refusal.
  Two ids that differ only by case are one file on a filesystem that ignores
  case (macOS's default): registering the second replaced the first's record,
  and is now refused with `FLX-7008` naming the first.
- The accuracy store no longer follows a symbolic link. A link planted at a
  record's name inside the store sent `register` through it, so it could
  replace another prediction's sealed record. Records are now read and written
  relative to their directory, and the store's directory
  (`<state-dir>/accuracy`) and the `predictions` and `verdicts` directories in
  it are opened with `O_NOFOLLOW`; a link, or the wrong type of file, where a
  record or one of those directories belongs is refused with the new
  `FLX-7009`, wherever the link points, and the file it points at is not
  touched. The state directory itself may still be reached through a link. A
  sealed record is published with `link` from a staged file, so it appears
  whole or not at all; a verdict is written through a staged file and one
  `os.replace`, which replaces a link at its name instead of writing through
  it. Records written by earlier builds keep their bytes and verify.
  The seal is a digest, not a signature: the check catches a record changed
  without being sealed again, and the digest `register` prints is what a third
  party checks a record against.

### Development

- An import-linter contract forbids `flux_stats` the whole engine, and the
  modules the kernel may not use (`random`, `uuid`, `socket`, `time`, `os`,
  `secrets`, `hashlib`), so the profiler can import it alone. The kernel's AST
  scan reads it too. The CI job `flux-stats alone` installs its wheel and its
  test group where no engine is installed and runs its tests, and
  `make packaging-smoke` installs the flux-stats wheel alone, with no index,
  and reads the example profile.
- The release workflow builds, attests and attaches four files, the wheel and
  the sdist of each distribution, and uploads flux-stats to PyPI before the
  engine. `scripts/release_guard.py` refuses a tree or a tag where the engine's
  version, flux-stats's version and the engine's `flux-stats==` pin disagree.
  The first-run check runs the engine wheel it builds with the flux-stats wheel
  built beside it.
- The weekly mutation run covers the statistic registry, at a 95% floor for
  each of its two modules. `tests/concordance/test_statistic_diagnostics.py`
  pins every refusal the record reader can raise, column, row and value class
  included, which took `statistic.py` from 90.6% to 97.8% once the arithmetic
  had moved out, so its floor went up from 90% to 95%.
- The weekly mutation run covers `flux_stats.reading` at the reader's 95%
  floor, held by flux-stats's own tests (`packages/flux-stats/tests/test_reading.py`),
  which pin every refusal the reader raises and compare the instant parser
  with the `datetime.fromisoformat` reading it replaced, on Python 3.11 to 3.13.

- The sinks tests skip where pyarrow cannot import. pyarrow 26 needs NumPy 2.0
  or newer, raises `ImportError` at import on NumPy 1.x, and does not declare
  the requirement in its wheel, so pip installs the pair without a word. The
  nightly's NumPy 1.26.0 determinism cells install `.[dev]`, pin NumPy and
  collect the whole tree for `make test-vectors`. pytest 9.1 skips only on
  `ModuleNotFoundError`, so the sinks modules failed at collection and the
  step that holds the pinned digest set exited non-zero. `pyarrow_or_skip`, in
  `tests/sinks/conftest.py`, skips below NumPy 2 and lets an import failure
  stand from NumPy 2 on, where a pyarrow that will not import is a broken
  environment. `dev` asks for pytest 8.2 or newer, the first release whose
  `importorskip` takes `exc_type`. No engine code, digest or golden moved.
- The empty `flux_engine.accuracy._scoring` package is gone, with the
  import-linter contract that fenced it ("accuracy scoring mechanism is not
  wired until v0.3") and the test that pinned it empty. It held a place for a
  proper scoring rule planned for v0.3, which no release has built; the
  engine scores a prediction by plain containment.
- The CLI is one module per command. `flux_engine/cli/__init__.py`, 2,368
  lines, keeps the parser's root, `main` and the dispatch; each command, its
  `configure(sub)`, its worked examples and the functions `main` calls, is a
  module under `flux_engine/cli/commands/`, listed in the order `--help` shows;
  the parser class, the shared flags and the output are `cli/_parser.py`,
  `cli/_args.py` and `cli/_output.py`. `tests/cli/test_help_snapshots.py` holds
  every parser's arguments and every command's `--help`, recorded before the
  split, and `make help-snapshots` rewrites them after a deliberate change. The
  help, the usage errors with and without `--json`, `--version`,
  `describe --self --json` and both entry points print the same bytes as
  before on Python 3.11 to 3.14.

## 0.5.0 - 2026-11-14

A FLUID `INTEGER` column takes a drawn count, a fidelity column can be drawn
given another, the fidelity check counts two categorical columns together,
`validate` and `plan` check a fidelity block before any run exists, and
`plan.json` records every fidelity block's epsilons. That last change moves
the `planDigest` of every bundle that declares a fidelity block, and
`GOLDEN-CHANGELOG.md` has one new row for it. A bundle without a block keeps
every digest. `KERNEL_VERSION`, `SEED_TREE_VERSION` and `TRACE_VERSION` keep
their values.

### Added

- A fidelity column can be drawn given another column of the same expose
  (SEAM-FIDELITY-SPEC §2). `given` names that column, and one weight table per
  level of it takes the place of the column's own weights. A histogram column
  declares `binWeightsGiven`: one `binWeights` list per level, which a grouped
  `marginalHistogram`'s (group, bin) counts state directly. A categorical
  column declares `levelWeightsGiven`: one mapping of level to weight per
  level. The links form a forest: a column has at most one `given`, a cycle is
  refused, a histogram column is a leaf, and each column is drawn after the
  column it is given, whatever the declared order. Every cell keeps its own
  stream, so column-first generation and slices are unchanged, and a root
  column (one drawn given nothing) draws exactly the values it drew without
  the links. A declaration the generator cannot draw is refused at plan
  time with `FLX-3402`. A plan without `given` documents none of the new keys,
  so its rows and its `generatorDigest` do not move.
- `flux_engine.calibrate.structure` suggests which column each column is drawn
  given, offline and from two-way count tables alone. It builds a Chow-Liu tree
  (Chow and Liu, 1968), the selection step of the MST synthesizer (McKenna,
  Miklau and Sheldon, 2021) without its privacy noise, with every numeric
  column a leaf, and `declarations` writes the matching `columns.<name>`
  entries.
- A how-to, "Draw a column given another", declares `given` from a grouped
  histogram's counts and shows the fidelity gate catch the link missing.
- A `levelHistogram` fidelity property (SEAM-FIDELITY-SPEC §2, §4 and §5)
  counts a support column's records per declared level. Grouped by a second
  support column, it commits a two-way count table of two categorical columns
  into S, and the check compares the synthetic table with it group by group,
  in the total-variation form. Its cells are the declared levels, so a value
  outside either column's declared support is refused with `FLX-5005`, and a
  group below `records.min` is refused as every group is.
  `pair_table_from_level_groups` reads its counts back as a two-way table for
  `suggest_tree` and `declarations`. A bundle that declares no
  `levelHistogram` keeps its S, its digests and its rows.
- What the fidelity check covers: every declared link. A histogram drawn given
  a categorical is checked by a grouped `marginalHistogram`, and a categorical
  drawn given a categorical by a grouped `levelHistogram`.
- `fluxsim verify --json` names each declared property of a fidelity check that
  did not land within its margin, under `artifacts.fidelity.<exposeId>.outside`:
  `propertyId`, `property`, `columns` and `refused`, and for a grouped
  property `groupBy` and `groupOrdinals`, the zero-based ordinals of the groups
  that landed outside, in the sorted group order that the `group #N` of an
  `FLX-5005` refusal uses. A property the check refused is listed too, as the
  fidelity record's `outside` row lists it, with `refused` true, and its
  `refusals` entry says why. A group is named by its ordinal and never by its
  key. Before, a property outside its margin left only `"status": "failed"`.
  The list is there whenever the check ran, and empty when it passed.
- The published fidelity vectors, `tests/vectors/fidelity-vectors.json`, are
  `vectorsVersion` 2. `kinds.levelHistogram` is a block of its own, with a
  contract, a committed slice, the digests, plan fragment, counts and
  commitment they pin, and nine evaluation cases for the ungrouped and the
  grouped property: a total variation exactly on the margin and one record
  past it, a link lost with the channel counts intact, a declared level a group
  never holds, a null level under `null: exclude`, and the two refusals. Every
  version 1 key but `vectorsVersion` itself is what it was, and a test pins
  them by digest. A kind is a block of its own because a property added to
  version 1's block would move every digest that block pins. The coverage test
  now asks that the published blocks together name every property kind, and the
  exception that pinned `levelHistogram` outside the file is gone.

### Changed

- `fluxsim describe --self` no longer reports a `synth` capability. ADR 0003
  rules record-level synthesis out, its 2026-11-11 amendment removed the
  `[synth]` extra, and nothing in the engine could use synthcity, so an
  installed copy read as a feature.
- A FLUID `INTEGER` column of a scenario-driven emit takes a drawn count, such
  as the towers `tenancyCount`. Whether the column's source is always a whole
  number is decided at plan time from the declared families: a rate count, a
  bounded normal at scale 0, a constant whole number, or an integer scalar such
  as `moment.eventCount`. A source that is not, such as an amount, stops
  planning with the new `FLX-3303`, where before the run failed on its first
  row with `FLX-3003`, or passed when it had no rows. The renderer still
  refuses a `Decimal` in an `INT` column from every other caller. KERNEL-SPEC
  §9 states the rule.
- `fluxsim validate --json` names the code of a refusal found in one
  Simulation, such as `FLX-4103` for `fidelity: stochastic`, as it already did
  for a refusal of the whole bundle, and the text output points to the code's
  page. It reported `null` before. Problems with different codes still report
  `null`.
- `fluxsim plan` writes every fidelity block it compiles into `plan.json`, as
  SEAM-FIDELITY-SPEC §3 says: "Both `H` and `G` and every `ε` are written to
  `plan.json`." The emit's entry carries the block under `fidelity`, the key
  the block is declared under: `exposeId`, `H`, `G`, the `encoding`,
  `fieldCapacityBits`, and per property its `epsilon`, `margin`,
  `maxNonMembers` and `magnitudeBits`. That is
  `CompiledFidelity.plan_document()`, the fragment the published vectors pin
  as `expect.plan`. Before, `plan` compiled the block and dropped the result,
  so a plan recorded no epsilon, and a reader checking a fidelity record later
  had no planned number to hold it against. The key moves the `planDigest` of
  a bundle that declares a block, and so its run id: a fresh plan lands in a
  new run directory. Its run's `runDigest` moves too, because `run-digest.json`
  records `planDigest`, so a `replay --assert-digest` pin taken from a fresh
  plan needs the new value. A recorded run replays to the `runDigest` it had.
  An emit without a block gets no key, so its `plan.json` and its digests stay
  as they were. A plan recorded by 0.4.0 still applies,
  verifies, replays and ships under this version, under its own run id: no
  stage after `plan` reads the key.
- The failed fidelity leg of `fluxsim verify` names the property. It printed the
  expose id and nothing else, on stdout and on stderr. It now prints one line
  per property that did not land within its margin, worded after `ship`'s
  lines, such as `property 003:mean:amount@market (mean of amount, grouped by
  market): outside the declared margin in group #1`, under the leg line and on
  stderr after `<exposeId>: fidelity check failed`, then one line per refusal
  with its code and reason. A line names fields, codes and group ordinals,
  never a record value. The `error.message` of the `--json` envelope is the
  same text. The refusal of `--update-golden` over a failed leg names them on
  its one line, and in `artifacts.golden.problems`, as `<exposeId>: fidelity
  check failed: property ...`.

### Fixed

- A group whose category is the empty string was held to the ungrouped floor
  of one record rather than to `records.min`, in every grouped fidelity
  property, because the ungrouped group's key is the empty string too. A
  one-record group could reach S. Whether a property is grouped is now read
  from its `groupBy`, so that group is refused below `records.min` with
  `FLX-5005` and named by its ordinal, as every group is.
- Two kinds of fidelity weights that sum to one failed on every draw with
  `FLX-5021` ("categorical weight ... is negative"): a histogram whose
  `binWeights` render to a little more than one in total and whose last bin
  is `0`, such as twelve bins of `2`, one of `13` and a last bin of `0`; and
  `levels` within `1e-9` of one whose NFC-last level is `0`. The rounding
  residue went to that last level, which had no mass to give. It now goes to
  the NFC-last level that can take it. A plan that generated before
  generates the same rows and the same plan digest.
- `binWeights` that are all zero are refused with `FLX-3402` when the plan
  is formed. Before, the refusal came on the first row drawn, so a plan with
  no rows passed.
- A categorical level that cannot be read as its column's type, such as `p`
  on a `DECIMAL(18,2)` column, is refused with `FLX-3402` when the plan is
  formed, whatever its weight. Before, the plan passed, and the first row
  that drew the level raised a bare `decimal.InvalidOperation`, or a
  `ValueError` for `NaN` on an `INT` column. A plan whose bad level is
  declared at `0` used to generate rows and is now refused too. Such a level
  is outside the column's domain, and when it is the NFC-last level it takes
  any positive rounding residue, so a row can still draw it. A `BOOL` level
  other than `true` or `false` follows the same rule and keeps its message.
- `fluxsim validate` checks a fidelity block's properties, as
  SEAM-FIDELITY-SPEC §2 says it does. It read only the block's columns, so a
  malformed property of any kind, such as a `levelHistogram` that carries
  `bins` or a `marginalHistogram` over a `support` column, passed it and was
  refused only by `verify` or `ship`, once a run and a committed slice
  existed. It now runs the parser `verify` uses, offline, and refuses the
  property with the code and the message `verify` gives (`FLX-5030`,
  `FLX-5032`, `FLX-5033` or `FLX-5034`), under the Simulation's id.
- `fluxsim plan` compiles every declared fidelity block before the population
  exists, as SEAM-FIDELITY-SPEC §3 says it does. A malformed property is
  refused there too, with the code and the message `verify` gives; so is a
  tolerance no larger than its encoding error (`FLX-5002`) and a comparison
  wider than its field (`FLX-5001`). `apply` and `run` plan the same way.
  Before, `plan` passed all of these, and the run wrote rows that no check
  could accept. A bundle that planned before still plans, to the same rows.
  Its `plan.json` also records the compiled block now: see Changed.
- A number in a fidelity block that has more than 1000 digits written out,
  such as a range bound of `"1E+99999999"`, is refused with `FLX-5030` by its
  length, before its value is formed. Forming it exactly took minutes, so
  `validate`, `plan`, `verify` and `ship` each stalled on twelve characters.
  1000 is jackson-core's default longest number. It also keeps what an
  accepted number becomes inside CPython's 4300-digit limit on printing an
  `int`: a `maxStateAge` or a tolerance of about 4300 digits made `ship` or
  `plan` stop with a bare `ValueError` in place of its refusal. A block whose
  numbers all have 1000 digits or fewer reads exactly as before. A range
  bound that a property reads already failed the field-capacity check
  (`FLX-5001`) long before that length, but a tolerance or a quantile `level`
  that long used to compile, and is now refused.
- An unquoted integer of more than 4300 digits in a bundle document, or in
  the `flux.lock` that a versioned ref makes the validator read, is refused
  with `FLX-1003`, the code an integer outside the JSON safe-integer domain
  already gets when its digest is taken. PyYAML builds an integer with
  `int()`, and CPython converts at most 4300 digits, so `validate` and `plan`
  stopped with a bare `ValueError` before any reader ran. A value that long
  spelled in hex loaded, and then broke the first message that printed it; it
  is refused the same way. The refusal names the file, the line and the
  column, never the value. A scalar that YAML reads as an integer but that
  has no integer value, such as `0b_` or `!!int ''`, is refused with
  `FLX-3105` in place of a bare `ValueError` or `IndexError`. A lockfile the
  validator never reads still has no say in whether a bundle loads.
- A draw parameter that only the generator reads, such as
  `mean: "1E+99999999"`, is refused by `plan` with `FLX-5030`, by the same
  1000-digit rule and with the same message as a number the check reads.
  Before, the check never saw it, and `plan` stopped in about a second on an
  uncaught `decimal.Overflow` while it drew rows. `validate` does not read
  draw parameters; `plan` is the first command that does.
- A `fidelity` value that is not a mapping, such as `fidelity: oops`, is
  refused with `FLX-5030` ("fidelity must be a mapping") by `validate`, `plan`
  and every command after them. It counted as no block: `validate` passed it,
  and `verify` reported the emit `not declared`. A null value still declares
  no block, as a null extension namespace or entry does.

## 0.4.0 - 2026-11-12

A second vertical, `towers`, and FLUID 0.7.6. No telco digest moved:
`GOLDEN-CHANGELOG.md` gains one row, for the new `tests/golden/towers-public/`
set, and `KERNEL_VERSION`, `SEED_TREE_VERSION` and `TRACE_VERSION` keep their
values.

### Added

- The `towers` vertical pack: a tower company whose subjects are its tenants,
  one operator in one market. Subject attributes `tenancyCount`,
  `monthlyRecurringCharge`, `slaTargetPct`, `chronicSlaBreaches` and
  `monthsToLeaseExpiry`; the signals `sig-sla-breach`, `sig-site-outage` and
  `sig-billing-dispute`; emit aliases for the `tenant_renewal_risk_moment`
  contract; and markets named by ISO country code, so a tenant's market is its
  country. Its derivation draws more chronic breaches and fewer months to lease
  expiry for at-risk and notice-giving tenants. `fluxsim plugins` lists it.
- Arrival shapes per vertical (`flux_engine.runtime.signals.ARRIVAL_MODELS`). A
  tower tenant's breaches arrive days apart, so the towers gap mean is nine
  days. A vertical with no row, telco among them, keeps the default.
- FLUID 0.7.6. The vendored specification (`flux_engine/_vendor/flux_spec`) is
  re-synced to FLUX 0.5.1, which accepts an exact `fluidVersion: "0.7.6"` pin
  and vendors the FLUID 0.7.6 schema. An installed `flux-spec` still takes
  precedence: running a 0.7.6 contract needs a `flux-spec` that carries
  FLUID 0.7.6.
- A golden baseline for the FLUX repository's `examples/towers-tenant-renewal`
  bundle, including the `runDigest` that `fluxsim replay --assert-digest` must
  reproduce.

### Changed

- `fluxsim describe --self` reports `0.5.1` when the vendored specification is
  the one running, and `docs/reference/schemas.md` links each schema family's
  current file (FLUX 0.5.1 kept the 0.5.0 enforcement and manifest schemas).
- CI installs `flux-spec` from the sibling FLUX checkout in the jobs that read
  its example bundles, so an example and the validator that checks it come
  from one commit.
- The `flux-spec` floor is `0.5.1` (`flux-spec>=0.5.1,<0.6`), the first
  release that carries FLUID 0.7.6. An older installed `flux-spec` refuses the
  towers example's `fluxVersion: "0.5.1"` documents (`FLX-3105`).

## 0.3.1 - 2026-11-12

A fix for the HTTP lineage emitter. No pinned digest moved:
`GOLDEN-CHANGELOG.md` has no new row, and `KERNEL_VERSION`, `SEED_TREE_VERSION`
and `TRACE_VERSION` keep their values.

### Fixed

- `OpenLineageHttpEmitter` delivers its events. Since 0.2.0 it handed the
  OpenLineage client a plain dict, which the client refuses, and swallowed
  the error, so with the `[lineage]` extra installed no event reached the
  receiver: `failed` counted each one and `flush()` returned `False`. Events
  now go through the client's HTTP transport, and the body leaves out null
  values, which the NDJSON lines keep. `OPENLINEAGE_DISABLED` still turns
  delivery off.

### Development

- A `Lineage extra` CI job, and `make lineage-extra`, run the lineage tests
  against the real client. `make ci-local` runs that target first and needs
  the extra: `uv sync --extra dev --extra lineage`.

## 0.3.0 - 2026-11-12

Output sinks, and generation several times faster. No pinned digest moved:
`GOLDEN-CHANGELOG.md` has no new row, and `KERNEL_VERSION`, `SEED_TREE_VERSION`
and `TRACE_VERSION` keep their values. Every row, line and digest is the one
0.2.1 produces.

### Sinks

- `fluxsim run --sink NAME:TARGET` writes every stream of a run to a sink,
  after verify and before ship, and records each delivery in
  `sink-receipts.json`; `--sink-opt KEY=VALUE` passes a sink its settings, and
  `fluxsim plugins` lists the sinks.
- `parquet:DIR` and `arrow-ipc:DIR` write files named by the stream's
  `outputDigest`, behind the new `[arrow]` extra.
- `adbc:DRIVER` loads a stream into a database through ADBC, behind the new
  `[adbc]` extra; `uri-env=VAR` keeps a connection string off the command line.
- A how-to, "Load a simulation into a database", and `make bench`, which
  measures what each sink adds to a run.

### Faster generation

- One process makes the same rows in under half the time and a fraction of
  the memory: 100,000 rows of the small telco fixture took 21.4 s and 511 MB,
  and now take 8.3 s and 127 MB.
- `--jobs N`, or `--jobs auto`, on `plan`, `apply`, `run` and `replay` (and
  `FLUX_JOBS`, and `jobs=` in the Python API and the async facade) makes an
  emit's rows in up to N processes and joins them in row order, so the output
  is the one a single job gives. The same 100,000 rows take 1.8 s with 12 jobs.
  Runs in one process share one worker per CPU, a small emit stays in one
  process, and the run summary says how many processes made an emit.
- Workers start with the current directory off their import path and end
  with the process that started them. A script that passes more than one job
  needs an `if __name__ == "__main__":` guard. `fluxsim` sets
  `PYTHONSAFEPATH` for its whole process, so a Python script that a sink or
  plugin starts as a child process does not have its own directory on its
  import path either.
- New public helpers: `core.canonical.canonical_array_bytes`,
  `core.seedtree.stream_entropy_of_label` and `stream_of_label`,
  `core.rng.equal_weights` and `clear_memos`, `generator.paths.CellStreams`,
  `generator.generate_row_slice` and `seam.emit.EmitWriter.from_framed`.

## 0.2.1 - 2026-11-11

Docs and wording fixes; no behaviour change. No pinned digest moved:
`GOLDEN-CHANGELOG.md` has no new row, and `KERNEL_VERSION`, `SEED_TREE_VERSION`
and `TRACE_VERSION` keep their values.

- The README and the tutorial no longer say the package is unpublished or that
  you need a clone and a built wheel, so the PyPI page shows the install line
  that works.
- `fluxsim explain FLX-8030` and its page say "generated evidence prose contains
  a reserved word" and point at the list in `flux_engine/_vocabulary.py`. The
  check itself is unchanged.
- The specifications under `docs/spec/`, the ADR status lines and a few
  comments lose wording that was only meant for the people building the engine.
- The accuracy report links the nightly workflow instead of a run that no longer
  exists.
- `SECURITY.md` supports 0.2.x, `docs/RELEASING.md` describes Trusted Publishing
  as live, the release notes template has one Install section, and
  `CONTRIBUTING.md` says the sibling FLUX clone is optional.
- Packaging metadata: `Documentation` and `Issues` project URLs, and
  `CITATION.cff` carries the version, the release date and the licence.

## 0.2.0 - 2026-11-11

The command is now `fluxsim`, the distribution is now `flux-sim-engine`, and
the licence is Apache-2.0. The engine also gets documentation and an accuracy
program, which found four defects; all four are fixed here. No pinned digest
moved: `GOLDEN-CHANGELOG.md` has no new row, and `KERNEL_VERSION`,
`SEED_TREE_VERSION` and `TRACE_VERSION` keep their values.

- Renamed the distribution from `flux-simulation-engine` to `flux-sim-engine`.
  The import package stays `flux_engine`, and the product keeps its name, the
  FLUX Simulation Engine.
- Changed the licence to Apache-2.0, declared with PEP 639 metadata (`license`
  and `license-files`). `LICENSE`, `NOTICE` and the notice of the vendored
  `flux_spec` copy go into the wheel and the sdist.
- The engine now depends on `flux-spec>=0.5,<0.6` from PyPI instead of a pinned
  git commit, so the package can be installed from an index. The pin-drift
  alert in CI went with the pin.
- Renamed the command from `flux` to `fluxsim`, with no alias. `flux` is the
  command line of Flux CD, and the two collide on a shared `PATH`. The console
  script, `--help`, error messages, the docs and the examples all say
  `fluxsim`. The `FLUX_*` environment variables, the `.flux/` state directory
  and the `flux_engine` package keep their names: they name the engine and the
  specification, not the command. Nothing digest-bearing changed, so no golden
  moved. Text that names the command moved with it, so a host that matches on
  it should expect that: the `stage` of a `--json` envelope for a usage error
  at the top level, before any subcommand, is now `fluxsim`; the
  `error.message` of a usage error starts `fluxsim ...:` instead of
  `flux ...:`; the remediation text in error messages and in `fluxsim explain`
  entries says `fluxsim` where it said `flux`; and so do the `help` strings in
  the command tree of `fluxsim describe --self --json`. The `FLX-nnnn` codes
  themselves did not change.
- Added `fluxsim init <dir>`. It writes a small FLUX bundle (nine documents)
  and its FLUID contract into a directory that is missing or empty, and never
  overwrites anything, so a first run needs no sibling checkout of the
  specification. `fluxsim run . --verify --ship -o out` inside it gives a
  verified run and an evidence pack in seconds. The starter ships as package
  data (`flux_engine/starter/`), and its result is pinned by
  `tests/starter/test_starter_bundle.py`.
- Added `make first-run` and a `First run` workflow. They build the wheel and
  run that path through `uvx` from outside the checkout, with an empty uv
  cache, then check the evidence pack and record the time to first success.
  Linux runs on every pull request; macOS runs weekly. The job is advisory: it
  is not one of the jobs `CI gate` waits for.
  `make packaging-smoke` now also runs `init` and the run from the installed
  wheel.
- Fixed the evidence report naming no bundle when the bundle was given as `.`:
  it began `# Evidence: ` with a blank where the name goes.
- Fixed `-o` pointing at a directory inside the bundle (`fluxsim run . --ship
  -o out` from the bundle directory): the next archive packed the previous
  one, and every re-run grew it. The output directory is left out of the
  staged copy.
- Removed the `calibrate` and `synth` extras. Nothing in the engine imported
  what they installed, except that the default experiment readout used `gbstats`
  whenever it happened to be installed, so the same run could read out
  differently on two machines. `GbstatsReadout` is gone. The readout is now
  always the exact two-proportion interval, with no switch that depends on which
  packages are installed.
- Fixed: `accuracy score` read a registered interval bound of the JSON
  number `0` (or `0.0`) as no bound, so the prediction was scored
  `not-scored` instead of `within-interval` or `outside-interval`. It
  now reads the bounds the way `accuracy register` does.
- Fixed: `accuracy record` published, for every prediction with an
  interval, a digest that was not the `sealDigest` `accuracy register`
  returned, so the record could not be matched to its seals. It now
  rebuilds each prediction with its interval before digesting it.
- Fixed: a run read the caller's ambient `decimal` context in three
  places (the arrival gap rate, a scalar Persona trait's quantisation
  and the equal Persona share). A library caller at a precision other
  than 28 got different events and a run digest that did not replay,
  and at precision 5 the run raised. Those sites now use the kernel's
  pinned context, and `stages.pipeline.execute` runs the whole pipeline
  inside it. Under the default context nothing moves.
- Fixed: a draw whose value or bounds need more digits than the kernel's
  50-digit working precision at the declared scale (for example a
  Persona trait `{dist: normal, stddev: 1.25e43}`) raised a bare
  `decimal.InvalidOperation`. It is now refused with `FLX-5021`.
- Reworked `--help` after clig.dev. Examples come first, printed verbatim (a
  long one continues with a backslash, so it can be pasted), then the options,
  then a link to the command's reference page; the top level also says where to
  report a problem. A typo in a command name suggests the command, a missing
  argument shows that command's worked example, and leftover arguments are now
  reported by the command they were typed to rather than by the program: the
  `stage` of a `--json` usage error for `fluxsim validate . --bogus` is
  `validate`, not `fluxsim`. The command tree of `describe --self --json` is
  unchanged except for one full stop that became a semicolon in the help of
  `--committed-slice`.
- A failing command now says where to read more. A catalogued failure prints
  `more: fluxsim explain FLX-nnnn` and the address of that code's page; any
  other failure says how to get the traceback (`FLUX_DEBUG=1`) and where to
  report it. `fluxsim explain FLX-nnnn` ends with the same address. The
  `--json` envelope is unchanged. **The addresses point into the engine's
  documentation, which is published with the specification's site at
  https://agenticstiger.github.io/flux/engine/.**
- On a terminal, a command in the six-step loop now ends with `next:` and the
  command to run after it, built from the bundle and run id it just used, with
  the flags the next command needs again (`--simulation-id`, `--zone`,
  `--committed-slice`). The lines go to stderr only, never under `--json`, and
  `FLUX_NO_PROGRESS` silences them like the progress lines.
- Added the reference pages for the command line and for every `FLX-nnnn` code
  under `docs/reference/`, generated from the argparse tree and the error
  catalog (`make docs-gen`). `tests/docs/test_generated_docs.py` fails when a
  page is stale, when a code has no page, and when an address the CLI prints
  names a page that does not exist.
- `scripts/register_lint.py` now requires a worked example for every command,
  where it used to require an epilog.
- Added the rest of the documentation under `docs/`, in the Diátaxis layout: a
  tutorial, five how-to guides and four explanations, beside the reference. The
  README is now a front door that points into them. The examples in the
  documentation and in the README run as tests in CI (Sybil), so a page that
  stops working fails the build.
- Added the accuracy program under `tests/accuracy/`: nine families of tests
  that measure whether what the engine does is right, not only whether it
  repeats itself. They cover fidelity metrics on aggregates, goodness of fit for
  every sampler and declared distribution, the fidelity gate's own accuracy,
  metamorphic and schema-driven properties, seed-tree stream quality, journey
  conformance, a determinism matrix and speed, and the last family renders the
  report. A pull-request tier runs at fixed seeds; a nightly tier runs at fresh
  ones. The design is `docs/accuracy/DESIGN.md` and the committed report is
  `docs/accuracy/REPORT.md`. The program is test-only and changes nothing at
  runtime. It found the four engine defects fixed in this release.
- Added a CycloneDX SBOM of the installed wheel to the release assets, and a
  release job that publishes to PyPI through Trusted Publishing, with
  attestations. It runs only when the repository variable `PYPI_PUBLISH` is
  `true`. Every action in the workflows is pinned by commit, and
  `tests/test_workflow_pins.py` keeps it that way.
- Added the community files (`CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`,
  `SECURITY.md`, issue and pull request templates), a DCO sign-off check and
  `make check`, a quick local run of the checks a pull request goes through.

## 0.1.0 - 2026-11-08

First release.

- Added a release pipeline: `.github/workflows/release.yml`,
  `scripts/release_guard.py` with its self-test, `tests/release/` and
  `docs/RELEASING.md`.
- Removed the `tags: ["v*"]` push trigger from `.github/workflows/ci.yml`.
  A tag now reaches the six-cell whole-suite matrix through that file's
  `workflow_call` hook, which the release workflow calls, so one tag runs
  the matrix once rather than twice.
