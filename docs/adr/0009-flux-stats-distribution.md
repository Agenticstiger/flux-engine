# ADR 0009: the statistics are a distribution of their own, flux-stats

## Status

Accepted 2026-11-16. **Amended 2026-11-17:** the record reader's pure half
moves into flux-stats too, as `flux_stats.reading`, and the fidelity block's
grammar follows as `flux_stats.fidelity` (see the amendments below).
**Amended 2026-11-18:** the exact YAML read of a bundle and the canonical form
move too, as `flux_stats.exact_yaml` and `flux_stats.canonical`, with PyYAML
as the optional `yaml` extra, and a bundle's files are held to size caps
before they are read.

## Context

ADR 0003, as amended on 2026-11-16, says the engine never reads a record of
the data a bundle imitates. A separate profiler reads records only where they
live and emits only aggregates. The profiler is the forge-cli extra
`fluid profile`, and it depends on the statistics alone, so the FLUX
repositories hold no data-access code.

The statistics were already one module: `flux_engine.stats` held one definition
per statistic, the column metrics and the profile document reader, and the
fidelity check's statistic and verifier went through it. It imported the
standard library and two kernel modules, `flux_engine.core.errors` (the
`FLX-nnnn` catalog) and `flux_engine.core.fixedpoint` (the fixed-point grid),
and an import-linter contract and a fresh-interpreter test held that closure.
But the only way to install it was to install the engine, with numpy,
pydantic, jsonschema, cryptography, flux-spec and the rest. A profiler that
runs next to the data should carry none of that.

## Decision

1. **Names.** The distribution is `flux-stats` and the import package is
   `flux_stats`. Both names were free on PyPI on 2026-11-16, and they follow
   `flux-spec` and `flux_spec`.
2. **What moves.** `flux_engine/stats` becomes `flux_stats`, and
   `flux_engine/core/fixedpoint.py` becomes `flux_stats.fixedpoint`. The
   catalog mechanism of `flux_engine/core/errors.py` (the bands, `register`,
   `FluxError`) becomes `flux_stats.errors`. `flux_engine.core.errors` stays:
   it re-exports those same objects and keeps the engine's list of the modules
   that own codes (`ensure_core_codes_loaded`). A process therefore holds one
   catalog and one `FluxError` class, every code and message reads as before,
   and `fluxsim explain` is unchanged.
3. **No aliases.** `flux_engine.stats` was never in a release, so nothing
   re-exports it, and `flux_engine.core.fixedpoint` goes with no shim. Each
   statistic has one name.
4. **Layout.** One repository, one uv workspace. `packages/flux-stats` holds
   its own `pyproject.toml`, a `src/flux_stats` package and its own tests. The
   root `pyproject.toml` declares it as a workspace member and resolves the
   engine's requirement to it, and one `uv.lock` covers both. A change that
   touches both lands in one pull request.
5. **Versions.** Lockstep. One `vX.Y.Z` tag releases both distributions at
   `X.Y.Z`, and the engine depends on exactly that release
   (`flux-stats==X.Y.Z`): its verdicts are computed by these definitions, so a
   range would pair an engine with statistics it was never tested with. The
   release guard refuses a tree or a tag where the engine's version,
   flux-stats's version and the pin disagree. The release workflow builds,
   attests and attaches four files, and uploads flux-stats to PyPI before the
   engine.
6. **Enforcement.** import-linter now has both root packages, and its
   flux_stats contract forbids the whole engine and the modules the kernel
   may not use (`random`, `uuid`, `socket`, `time`, `os`, `secrets`, and
   `hashlib`, which stays `core.canonical`'s). The kernel AST scan reads
   flux_stats too. A fresh interpreter that imports `flux_stats` loads nothing
   outside the standard library. The CI job `flux-stats alone` installs the
   wheel and its test group where no engine is installed and runs the
   package's tests. The packaging smoke installs the flux-stats wheel alone,
   with no index, and reads the example profile.

## Prior art

Checked on 2026-11-16 before choosing (search receipts in the pull request):

- **opentelemetry-python** keeps `opentelemetry-api` and `opentelemetry-sdk`
  in sibling directories with one `pyproject.toml` each, declares them in a
  root `[tool.uv.workspace]`, releases them at one version, and pins the API
  from the SDK exactly (`opentelemetry-api == 1.46.0.dev`).
- **pydantic** moved `pydantic-core` into the pydantic repository (the old
  pydantic-core repository is archived), declares it as a workspace member and
  pins it exactly (`pydantic-core==2.50.0`), without matching version numbers.
- **Apache Airflow** declares `airflow-core` and its other distributions as
  members of one root uv workspace.
- **LangChain** keeps `langchain-core` in `libs/core` with its own version and
  depends on it by range (`langchain-core>=1.8.0,<2.0.0`). That is the
  alternative this ADR does not take, for the reason in decision 5.
- The uv workspace documentation (members, `{ workspace = true }` sources, one
  lockfile) and PyPI's Trusted Publishing documentation (one publisher entry
  per project; a pending publisher for a new name reserves nothing).

## Consequences

- `pip install flux-stats` installs the statistics with no third-party
  dependency, once a release carries it. `pip install flux-sim-engine`
  installs both. The engine's core gains no third-party dependency.
- The name `flux-stats` was free on PyPI, and the engine's exact pin would
  have made a squatted release run code on any plain-pip install from source.
  The owner registered it on 2026-11-16, before the exact pin was published, with a
  placeholder 0.0.1 that holds no code. Before the first release that carries
  flux-stats, its trusted publisher has to name this repository's release
  workflow (docs/RELEASING.md).
- Installing from a checkout with pip names the member first
  (`pip install -e packages/flux-stats -e ".[dev]"`); `uv sync` needs nothing
  extra. CI, the nightly and the release workflow do the former.
- mutmut 3.8.1 names a mutant by its path and strips only a `src/` prefix, so
  the weekly run mutates flux-stats from `packages/flux-stats`, against that
  package's own tests, and the engine's modules from the root as before. The
  floors of `flux_stats.fixedpoint`, `flux_stats.definitions` and
  `flux_stats.registry` are now held by the suite that ships with the package.
- flux-stats supports Python 3.10 and newer, one release below the engine's
  floor (the maintainers, 2026-11-16): `fluid profile` runs inside forge-cli, which still
  supports 3.10. The CI job `flux-stats alone` runs on 3.10, so nothing newer
  creeps in, and the floor rises when forge-cli drops 3.10.
- `--committed-slice`, the engine's one path that reads records, stays in the
  engine until `fluid profile` can profile a slice file.

## Amendment of 2026-11-17

The maintainers decided on 2026-11-17 that the record reader's pure half moves
too, in a pull request of its own after the first.

1. **What moves.** `flux_stats.reading` holds how the fidelity check reads a
   record: the cell a column names, refusing an absent column (`cell`); a
   number as the exact rational it spells (`exact`); a range column's value,
   a number or an instant (`read_exact`), checked against the declared range
   and put on the grid (`encode_value`); a category as its NFC string
   (`category`); the declared levels in their one order (`level_cells`); the
   null policy (`NullPolicy`, `read_column`, `read_pairs`); and the groups
   (`group_keys`, `UNGROUPED`). With it go `nfc` and `parse_rfc3339_ms`,
   which it calls, `ValueDomain`, and the two codes it raises that the engine
   registered, `FLX-5022` and `FLX-5031`, with their titles and explanations
   unchanged. Each function asks a column only for what it reads: a name
   (`NamedColumn`) to read a cell or a category, a name and a domain
   (`ValueColumn`) to read a range column's value, and the five attributes of
   `Column` for the record-set readers. No signature carries one of the
   engine's compiled types, and a profiler, which declares no range, reads a
   value without inventing one.
2. **What stays.** The engine's names for these are imports of the same
   objects: `parse_rfc3339_ms` and `FLX_5022` in `flux_engine.core.clock`,
   `nfc` in `flux_engine.core.canonical`, `NullPolicy` and `ValueDomain` in
   `flux_engine.concordance.predicate`, `FLX_5031` in
   `flux_engine.concordance.errors`, and `encode_value`, `level_cells`, `Row`
   and `UNGROUPED` in the boundary's statistic module. Some forty engine
   modules and tests call them, and keeping the names keeps one
   implementation without touching any of them, as `flux_engine.core.errors`
   does for the catalog. Which columns a property reads and which definition
   measures them stays in the engine (`compute_property_statistic`), and so
   does the boundary, which reaches the records.
3. **One change of behaviour.** `parse_rfc3339_ms` reads the pattern's groups
   into the `datetime` constructor instead of calling
   `datetime.fromisoformat`, whose accepted forms differ between Python
   versions: 3.10 refuses `Z` and a fraction of other than 3 or 6 digits, and
   3.14 accepts hour 24. On 3.11 to 3.13, the versions the engine is tested
   on, it returns what it returned before, value for value and refusal for
   refusal, on the 198,219 inputs of the pull request's differential check,
   and it returns the same on 3.10 and 3.14. A date-time whose instant falls
   outside the years 1 to 9999 once moved to UTC
   (`0001-01-01T00:00:00+00:01`) used to escape as an `OverflowError`; it is
   now refused with `FLX-5022`.

Consequences of the amendment:

- `fluid profile` reads a value by calling these functions, so forge-cli
  implements no reading of its own.
- The weekly mutation run mutates `flux_stats.reading` with flux-stats's own
  tests, at the reader's floor of 95%.
- ADR 0003's one exception stands. The boundary still reads committed
  records inside the engine, and the engine depends on flux-stats, so
  installing the engine still installs a record reader.
- Two risks moved with the code. The first is closed: `exact` formed the
  `Fraction` of a decimal before anything bounded its size, so a cell such as
  `1e99999999999` took unbounded time and memory, as it did in the engine's
  reader. Since a follow-up on 2026-11-17, before forge-cli reads with the
  module and before any release carries it, `exact` refuses a number that
  needs more than 1000 digits written out (`FLX-5005`), counted by
  `digits_written_out`, the declaration reader's own count, before forming
  it. The second stays open: `nfc` normalises with the running Python's
  Unicode database (13.0 on 3.10, 16.0 on 3.14), so a string holding a
  character newer than one of two Pythons can be keyed differently by each.
  A follow-up records the Unicode version with a profile and with a run.
- The parser keeps one leniency of the call it replaces, so that no value
  changes: an offset's minutes are added to its hours, so `+00:60` reads as
  `+01:00` where RFC 3339 caps the minutes at 59. Tightening it is a change of
  behaviour of its own.

## Amendment of 2026-11-17: the fidelity block

The maintainers decided on 2026-11-17 that the fidelity block's grammar moves too,
in a pull request of its own after the reader's.

1. **What moves.** `flux_stats.fidelity` holds how a Simulation's fidelity
   block is read: the expose it is checked against (`ExposeSchema`,
   `DeclaredExpose`, `expose_from_contract`), the closed sets (`ColumnKind`,
   `PropertyKind`, `ToleranceKind`, `FailureAction`), the declarations
   (`Encoding`, `RecordsRange`, `ColumnDeclaration`, `Tolerance`,
   `PropertyDeclaration`, `Budget`, `FidelityBlock`), the one key set of a
   column (`COLUMN_KEYS`), `read_support_levels`, `parse_duration_ms`,
   `require_number_length` and `parse_fidelity_block`. With it go
   `declared_enum_values`, which reads the levels of an `enum(...)` type and
   which `support: declared` calls, and the codes the grammar raises, `FLX-3001`,
   `FLX-5030`, `FLX-5032`, `FLX-5033` and `FLX-5034`, with their titles and
   explanations unchanged. The module takes `nfc` and `parse_rfc3339_ms` from
   `flux_stats.reading` and `FluxError` from `flux_stats.errors`, which are the
   objects the engine already re-exported.
2. **What stays.** The engine's names for these are imports of the same
   objects: every name of `flux_engine.concordance.predicate`, the four codes
   in `flux_engine.concordance.errors`, and `FLX-3001` and
   `declared_enum_values` in `flux_engine.seam.contract`. No engine module or
   test changes its imports. Which columns a property reads and which
   definition measures them stays in the engine (`compute_property_statistic`,
   which also keeps the `records.min` floors and the property ids).
3. **The per-group measurement moves with it.** `measure_group` forms one group
   of one property from the parsed `PropertyDeclaration`, the declared columns
   and the grid: support with its `maxCardinality` refusal, a level histogram
   with its refusal of a value outside the support, a correlation from its
   pairs, and a mean, a histogram on edges put on the grid, or a quantile from
   the encoded values. It was the boundary's `_group_statistic`, which read
   nothing of its compiled property but the declaration. The boundary calls it,
   with the same codes and messages.
4. **No change of behaviour.** The code is moved, not rewritten, and
   `fluxsim explain` prints the same text for each code.

Consequences of the amendment:

- A profiler such as forge-cli's `fluid profile --bundle` reads a Simulation's
  fidelity block with `flux_stats.fidelity` and forms each group of a property
  with `measure_group`, so it implements no parser and no group reader of its
  own.
- The weekly mutation run gains `flux_stats.fidelity`, mutated with
  flux-stats's own tests. Its floor is the 75% target until a first run
  records its kill rate, and it rises when that rate is known.
- `import flux_stats` now loads the module, so every code it registers is in
  the catalog without the engine; the closure a fresh interpreter loads grows by
  one module and stays standard library only.

## Amendment of 2026-11-18: the bundle read

The maintainers decided on 2026-11-18 that flux-stats exports the read of a bundle
that the engine takes its digests over, so that forge-cli's profiler and the
engine read a bundle with one reader. flux-stats's core stays standard library
only, and an optional extra brings PyYAML.

1. **What moves.** `flux_stats.canonical` holds the canonical form of
   KERNEL-SPEC §2.1: `canonicalise`, `coerce_key`, `render_decimal`,
   `JCS_MAX_SAFE_INTEGER` and the codes they raise, `FLX-1001` and `FLX-1003`
   to `FLX-1007`, with their titles and explanations unchanged.
   `flux_stats.exact_yaml` holds the exact YAML read: `ExactLoader` (no float,
   no datetime, every number exact) and `FLX-3105`, with `load_documents`, the
   read of a text's documents, and `check_documents`, which holds a text to the
   read's bounds before another reader reads it. Both take text and open no
   file.
2. **What stays.** `flux_engine.core.canonical` re-exports those names and
   keeps the steps that need more than the standard library or that make a
   digest: the RFC 8785 bytes (`rfc8785`), the sha256 (`hashlib`), masking and
   field paths. `flux_engine.seam.resolve` re-exports `ExactLoader` and
   `FLX_3105`, and keeps which files a bundle reads, how it opens them, and the
   budget each of the bundle's two reads shares across its files.
3. **The extra.** `flux_stats.exact_yaml` imports the standard library alone,
   so `import flux_stats` loads nothing else and still registers every code.
   PyYAML is imported when a document is first read or `ExactLoader` is first
   named, and without it either raises an `ImportError` that names
   `pip install 'flux-stats[yaml]'`. The engine depends on PyYAML itself, so its
   requirements do not change. PyYAML is in flux-stats's `test` group, so the
   `flux-stats alone` job runs the read's tests.
4. **Bounds.** The read carries over the bounds forge-cli's port of it gained
   in review. A document may write out at most 1,000,000 values once its
   aliases and merge keys are written out, and nest at most 64 levels, counted
   through its aliases; a document that holds itself through an alias is
   refused. The merge keys of one read of a bundle copy at most 100,000 pairs.
   A number needs at most 1,000 digits written out. Text nested too deeply for
   the parser is refused instead of raising `RecursionError`. Each bound
   refuses with `FLX-3105`, naming the file and the line, never a value. The
   largest bundle in this repository writes out 1,106 values, nests 11 levels
   and uses no merge key, and a bundle within the bounds reads to the same
   documents as before. The read also bounds the mappings the merge keys of a
   document name (1,000,000, each counted for every mapping that holds the key)
   and the characters a document writes out (64 MiB), since a string is one
   value however long it is and a number is spelled in full: it counts as the
   digits its decimal is spelled in when that is more than its text, and a zero
   counts the digits its scale spells, since the canonical form keeps the
   scale (`digits_written_out`, which the fidelity block's readers share). The
   main branch of the FLUX validator holds a document to the same bounds since
   2026-11-19 (Agenticstiger/flux#37, not yet released), counting a number as
   its text.
5. **The validator's read.** The FLUX reference validator reads a bundle with
   a plain SafeLoader, which bounds nothing in any release so far. So before
   it reads a file, the engine holds the file's text to the bounds with
   `check_documents`, decoded as the validator decodes it (in the locale's
   encoding, as `Path.open()` opens it): a file decoded two ways can hold a
   document that one decoding shows and the other hides in a comment. The
   check also builds each integer, float and boolean scalar as that read
   would, so one it cannot build is refused by its position. A file that does
   not decode, or that holds a character YAML does not allow, is refused
   there, because the validator meets either only where the file holds it,
   after building every document before it. A path that is not a regular file
   is refused without being read.
6. **File sizes.** Each read takes a file whole, so the maintainers decided on
   2026-11-18 that a bundle's files are held to two caps by their sizes,
   checked before a byte is read: 4 MiB for one file (`MAX_FILE_BYTES` in
   `flux_engine.seam.resolve`, the cap forge-cli's profiler holds a bundle's
   files to), and 256 MiB for its documents and lockfile together
   (`MAX_BUNDLE_READ_BYTES`, flux_spec's cap on an archive's members), so a
   directory and the archive packed from it meet one limit. Both are
   constants, as every other bound is. `load_bundle` takes every file's size
   with `stat` before it opens any, and each read (`read_bundle_file`) takes
   the file's `stat` again, refuses a path that is not a regular file, such
   as a FIFO or a device, with no byte read, and reads at most one byte past
   the cap. Past either cap, the read refuses with `FLX-3105`, naming the file
   and its size or the bundle's total. In an evidence archive,
   `verify_evidence` holds each document, lockfile and pack product to the
   file cap by the size its header declares, before flux_spec reads it, and
   flux_spec's own cap of the same size holds the archive's members together.

Consequences of the amendment:

- forge-cli's `fluid profile --bundle` reads a Simulation with these modules
  once a flux-stats release carries them, and its `profile` extra asks for
  `flux-stats[yaml]`, so its own port of the read goes.
- The flux_stats import-linter contract does not change. `exact_yaml` imports
  `sys`, for CPython's limit on an integer's digits (4300 on Python 3.10.0 to
  3.10.6, which have none), and PyYAML only inside functions.
- The check and the validator open a file separately, so a file changed
  between the two is read unchecked. A bundle is read from a directory the
  person running the engine controls, which this accepts.
