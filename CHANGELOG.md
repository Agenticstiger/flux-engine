# Changelog

What changed in the **flux-sim-engine package**, release by release.

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
