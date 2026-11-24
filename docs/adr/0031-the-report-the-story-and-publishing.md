# ADR 0031: the report draws the population, `fluxsim story` tells one entity's history, and publishing writes to object storage and a catalogue

## Status

Proposed 2026-11-22, for review. The evidence report gains a section, the CLI gains a command, and
the sink registry gains writers behind a new `[publish]` extra. No run digest and no file of a run
moves: the streams, the plan, the receipt and every existing sink's receipt keep their bytes.
`EVIDENCE.md`, which `ship` writes, does change for every run shipped with a profile, since it gains
the section. It is prose that no reader parses, and the evidence pack carries no format version of
its own, so none is bumped.

**Amended 2026-11-23:** a label keeps the letters and numbers of any script, as the
[amendment](#amendment-of-2026-11-23-labels-in-any-script) at the end sets out.

**Amended 2026-11-24:** shipped in 0.10.0, and now Accepted.

## Context

`EVIDENCE.md` (`flux_engine/evidence/report.py`) already states what a run is, its conformance,
its reproducibility, its fidelity, its privacy grade, each column's provenance and what was not
proven, with every assumption and its reason. Three things are missing: the population itself
against its targets, a way to read one entity's history out of a run, and a way to put a run where
its consumers read data, an object store or a table catalogue, without credentials passing
through the engine's records.

Sinks already exist on `apply` and `run` (`flux_engine/sinks/registry.py`): built-in sinks are
names imported only when selected, third parties register under the `flux_engine.sinks` entry-point
group, a receipt records each published stream, and the ADBC sink takes its connection string from
an environment variable (`uri-env`), records it as a label of its non-secret parts
(`target_label`), and scrubs its secrets from driver errors.

Borrow-before-build (2026-11-22):

- **Mermaid's XY chart.** Fetched `packages/mermaid/src/docs/syntax/xyChart.md` from mermaid-js on
  its `develop` branch: bar and line series over a categorical x axis, a numeric y axis with an
  optional range, a title; `xychart` is the current keyword and the documentation's own examples
  still use `xychart-beta`; named series and their legend need Mermaid 11.17.0. Searches "GitHub
  markdown mermaid xychart-beta supported render" (standard and extended): GitHub's documentation
  says diagrams render in Markdown files, issues and pull requests from a `mermaid` code block, and
  asks readers to check the Mermaid version in use, but it names no version; third-party guides say
  `xychart-beta` renders there. No first-party statement was found, so the report must read well
  where the chart is not drawn. `mermaid-py` 0.9.0 (MIT) renders through a web service and is not
  used: the report writes text.
- **Lake writers.** PyPI's JSON for each, as recorded on 2026-11-16 and still the latest release:
  `pyiceberg` 0.12.0 (Apache-2.0, 2026-09-01), `deltalake` 1.6.6 (Apache-2.0, 2026-09-24),
  `adbc-driver-manager` 1.13.0 (Apache-2.0, 2026-10-26), `obstore` 0.12.1 (MIT, 2026-10-27). obstore
  and the ADBC driver manager need Python 3.11, as the engine does.
- **Their credentials.** Search "obstore S3Store credentials environment variables
  credential_provider": every obstore store constructor reads its environment variables, a
  credential provider (one wrapping a boto3 session, for example) can supply credentials, and keys
  can also be passed as arguments. Search "pyiceberg configuration PYICEBERG_CATALOG environment
  variables .pyiceberg.yaml load_catalog credentials", fetched
  https://py.iceberg.apache.org/configuration: a catalogue comes from `.pyiceberg.yaml`, looked up
  in `PYICEBERG_HOME`, then the home directory, then the current directory, from
  `PYICEBERG_CATALOG__<NAME>__<KEY>` environment variables, or from properties passed in code.
  Search "deltalake write_deltalake storage_options environment variables": delta-rs reads AWS
  credentials from the environment, the shared configuration and credentials files, instance
  metadata, profiles and web identity when no storage options are passed.

## Decision

### 1. Population charts

`EVIDENCE.md` gains a section, "Population", after "Fidelity". For each histogram property that the
fidelity check scored (a `marginalHistogram/1`, `levelHistogram/1` or `shapeHistogram/1` property, and
a children-per-parent histogram where the scorecard scores one), it holds one chart, in a `mermaid`
code block, and the same numbers as a table under it:

```text
xychart-beta
  title "subscriber.market: bars profile, line run"
  x-axis ["DE", "FR", "NL"]
  y-axis "share (percent)" 0 --> 50
  bar [50.0, 30.0, 20.0]
  line [48.7, 31.2, 20.1]
```

- **Why the table.** These pages keep their own diagrams to a flowchart subset (`tests/docs/harness.py`),
  because GitHub draws Mermaid with a release it does not name and a diagram it cannot read becomes an
  error box. `EVIDENCE.md` is a report, not a page, so it may draw an XY chart; the table, inside a
  `<details>` element, keeps every number readable wherever the chart is not drawn.
- **Numbers.** A share is a cell's count over its group's total, exactly, written as a percentage
  rounded half to even to one decimal. The cells are the ones the score was computed over, so a
  withheld cell is left out as the check leaves it out. The y axis runs from 0 to the first multiple
  of 10 at or above the largest share.
- **Source.** `ship` draws the charts from the same pass that scores the properties. The scorecard
  keeps its format and its bytes; the score stays in `scorecard.json`, and the chart shows what it
  was computed over.
- **Bounds.** At most 32 charts, in property order, and at most 40 cells in one. A property with more
  cells is listed with its score and no chart. The section ends with how many of the scorecard's
  properties it left out, out of the scorecard's own total (a relationship's integrity or a mean has
  no histogram to draw), and it names a left-out property that is outside its tolerance with its
  distance and tolerance, so no failing property goes unmentioned.
- **Children per parent.** The chart of a `childrenPerParent` property is named by its property id
  (`children:invoice.account`) and has the profile row's bins as cells, the profile's parents as bars
  and the run's as a line.
- **Text from the data.** A title or a label keeps only ASCII letters, digits, the space and
  `_.,:;/()+=-`. Every other character becomes `_`, the text is cut at 40 characters, and it is
  written in double quotes. So no label can close the code block, start a Mermaid directive or
  comment, or carry markup. A label, a column or an emit name that would hold a reserved word once
  written is not printed: the cell is called `cell N` by its position, the column `column`, and the
  data keeps the real one, as the Provenance section withholds such a name.
- **Keyword and legend.** `xychart-beta`, which both older and current Mermaid read. The series are
  unnamed, since a legend needs Mermaid 11.17.0, so the title says which is which.
- A run shipped without a profile has the section with one sentence: there is no target to draw.

### 2. Consumer rates before and after calibration

When a run carries a consumer calibration record (the consumers' own decision record defines it),
the Population section adds one table: for each consumer query, its rate before calibration, after
it, and on the held-out queries apart, as the record states them, under the same text rules as
section 1. This record fixes where the rates are shown and how; the record's fields are its own.

### 3. `fluxsim story`

```text
fluxsim story --run-id RUN --emit EXPOSE --key VALUE [--depth N] [--limit N] [--state-dir DIR] [--json]
```

- **Attested bytes only.** It reads a committed run. Each stream it reads is first checked against
  the receipt's `outputDigest` with `verify`'s reader, so a story is told only from bytes the receipt
  attests. It reads the run directory alone, with the no-follow helpers, and writes nothing.
- **The entity.** The row of `EXPOSE` whose key equals `VALUE`, compared as the canonical text of the
  key's value; a composite key takes `--key COLUMN=VALUE`, once per column.
- **Its history.** The rows that link to it and the rows it links to, through the relationships of the
  contracts the run directory holds (`fidelity-input-<expose>.json`; `plan.json` records none), to
  `--depth` hops (2 by default), reading each stream once as a stream and keeping at
  most `--limit` rows (200 by default). Every row of `EXPOSE` that matches the key is the entity, and
  the entity's own rows come first (hop 0), the rest after them ordered by their first date or
  timestamp column, then by expose and row index. A first line says how many rows matched; then one
  line per row: its date, its expose, its key, its hop, the relationship it was reached by and its
  columns. A
  versioned expose shows each version with its validity and the tracked columns that changed, once
  versioned emits exist.
- **The terminal.** A control character in a value (C0, C1 or DEL), a bidirectional control (U+061C,
  U+200E, U+200F, U+202A to U+202E, U+2066 to U+2069) and the line and paragraph separators U+2028 and
  U+2029 are printed escaped, as `\x1b` or `‮`. So a value can neither drive the terminal nor
  reorder or split the lines a reader sees. `--json` writes the rows as JSON lines instead,
  each with its `hop` and `relationship`; a refusal under `--json` is the envelope every command prints.

### 4. Publishing sinks

Behind the `[publish]` extra, each a built-in name in `BUILTINS`, imported only when selected:

- **Object storage.** `parquet` and `arrow-ipc` take an object store URL (`s3://`, `gs://`, `az://`,
  `abfs://`) as their target and write through obstore, with the file names a local target gets. A
  local target is written as before.
- **`iceberg:<catalog>/<namespace>`** writes one table per stream, the `exposeId` inside the namespace
  (named as below), through pyiceberg. The namespace's levels are separated by `/`, never by a dot. By default the table is created and an existing one is refused; `--sink-opt mode=append`
  or `mode=overwrite` writes into an existing table.
- **`delta:<url>`** writes one table per stream at `<url>/<exposeId>`, through deltalake, with the same
  modes (`error` by default).
- **The bytes** are the ones `apply` committed, checked against their `outputDigest`: a sink publishes
  a stream after the check and from the checked rows.
- **Where they land.** A target's path is split on `/` into its prefix. An empty segment, `.`, `..`, a
  backslash and a percent-encoded separator (`%2F`, `%5C`, `%2E`, in either case) are refused, a trailing
  `/` is dropped, and the prefix is the remaining segments joined by `/`. A prefix of at least one
  segment is required, so nothing is written at a bucket's root. Each object's key is the prefix, a `/`, and the
  relative name a local target would get, and is checked to start with the prefix and a `/` before
  anything is written. That name is built from the `exposeId`, which FLUX's identifier grammar holds to
  letters, digits, `_`, `.` and `-`, neither starting nor ending with `.` or `-`
  (`IDENTIFIER_PATTERN`, `flux_engine/api/accuracy.py:50`), so it holds no separator. An Iceberg table
  is named by the tuple `(*namespace, exposeId)`, never by a dotted string, so an `exposeId` that holds
  a dot stays one table name and never becomes a level of the namespace.

### 5. Credentials

- **Where they come from.** Only the environment or the platform's own credential chain. The engine
  passes no credential to obstore, pyiceberg or deltalake, and the ADBC sink keeps `uri-env`.
- **Closed options.** Each new sink reads a closed `OPTIONS` tuple, as the ADBC sink does (`mode`, and
  for the file sinks their existing options). No option passes through to obstore's configuration,
  deltalake's `storage_options` or a catalogue's properties.
- **Endpoints.** An endpoint or URL comes only from the command line (`--sink`) or from the person's own
  environment and configuration (an endpoint variable, the catalogue file in the home directory or
  `PYICEBERG_HOME`), never from a bundle. A bundle names no sink: only `--sink` publishes, and no bundle
  key, plan field or environment variable selects a publishing sink. So ambient credentials are never
  sent to an endpoint a bundle chose, and a store that speaks S3 at another address still works through
  the person's own setting.
- **What is refused.** A `--sink-opt` whose key names a credential (`password`, `secret`, `token`,
  `credential`, `key` as a word of the key such as `access-key`, `sas`, `signature`), naming the key
  and never the value. A target that carries user information, a query or a fragment
  (`s3://user:pass@bucket/`, `?sig=`), shown with them removed. The same key rule applies to every
  sink, built-in or not.
- **A catalogue configuration beside a bundle.** pyiceberg also reads `.pyiceberg.yaml` from the
  current directory. The `iceberg` sink refuses to start when such a file is in the current directory
  and that directory is neither the home directory nor `PYICEBERG_HOME`, so a bundle directory cannot
  point a catalogue, and the storage requests it signs, at another endpoint by carrying a file.
- **Errors, logs and lineage.** A writer's error is reported as its class and its message, with every
  URL's user information and query removed and every value of an environment variable whose name names
  a credential replaced by `***`. The same scrubbing applies to everything the engine logs or records
  about a sink, lineage events included, and the engine raises its own error without the writer's
  exception chained to it, so no traceback carries a signed URL or a header.
- **Records.** No secret enters a digest, a plan, a receipt, lineage or `EVIDENCE.md`. A publishing
  sink's receipt records the target with credentials removed, the table, the snapshot or version the
  write made, each object's key with its size and the sha256 of its bytes (taken before the upload),
  and the writer and its version.

### 6. The extra

`publish = ["pyarrow>=18", "pyiceberg>=0.12.0,<0.13", "deltalake>=1.6.6,<2", "obstore>=0.12.1,<0.13",
"adbc-driver-manager>=1.13.0,<2"]`: floors at the versions checked, and below the next minor version
for the libraries before 1.0. `THIRD_PARTY_LICENSES.md` lists each with what it is for.

## Consequences

- Every evidence pack shipped with a profile shows its population against its targets. A pack shipped
  before this section existed is unchanged, and the scorecard's bytes do not move.
- `fluxsim story` reads only attested bytes and prints nothing a run does not hold.
- A run can be published where its consumers read, and the person who publishes it holds the
  credentials in their own environment. The engine never sees more than the platform's libraries
  read from there.
- Publishing is an outward write. Its default refuses to replace an existing table, and replacing one
  is a flag the person types.

## What would change this decision

- GitHub naming a Mermaid release that draws XY charts: the tables could go.
- A writer that cannot take its credentials from the environment: it is not added.
- Profiles that publish children-per-parent histograms per period: the section would draw them over
  time.

## Amendment of 2026-11-23: labels in any script

### Context

The text rule above kept only ASCII letters and digits, so every character of another script became
`_`. A level written 東京, 大阪 or 京都 printed as `__` in each of three rows, and two long labels
that shared their first 40 characters printed the same. The rule was safe and it made a report of a
non-English population unreadable.

### Decision

- **Kept.** A title or a label is put in NFC. It keeps the ASCII letters, digits, the space and
  `_.,:;/()+=-` as before, and the letters and numbers of any script (Unicode general categories `L`
  and `N`). A combining mark (`M`) is kept when it follows a kept letter, number or mark.
- **Replaced by `_`.** Control and format characters (the bidirectional controls, the zero width
  characters and the joiners among them), line and paragraph separators, every space but the plain
  one, private-use, surrogate and unassigned code points, and every punctuation mark or symbol outside
  the ASCII set above. So a newline, `#`, `|`, `[`, `<`, `"` or a backtick still never reaches the
  report, and nothing in a label can close the code block or the `<details>` element, start a heading,
  a link, a table cell, a Mermaid directive or comment, or carry markup. The reserved word check is
  unchanged.
- **Distinct labels.** A label that prints as it was given (after NFC) is unchanged, so an ASCII label
  of allowed characters and at most 40 of them prints exactly as before. A label that prints as
  something else, because a character was replaced or the text was cut, is shortened to 31 characters
  and ends in `_` and the first eight hexadecimal digits of the SHA-256 of the given text in NFC and
  UTF-8, 40 characters at most. A given text that already ends in `_` and eight such digits is treated
  the same way, so no two different labels print alike except by a collision of those 32 bits. The
  result depends on the text alone, so it is the same on every platform and in every run.
- **Where.** The one function writes every title, label, column, emit name and score text of the
  Population section. `fluxsim story` and the file names of a run do not carry these labels, and the
  data keeps the real label.

### Consequences

- `EVIDENCE.md` of a run shipped with a profile whose levels have letters outside ASCII changes, as
  it did when the section was added. No run digest, plan digest or file of a run moves, and the
  scorecard keeps its bytes.
- A label that holds an ASCII character the rule replaces, such as `a|b`, now ends in the eight digits
  where it used to end at the `_`.
- A label of letters that look alike in two scripts (a Cyrillic and a Latin `a`) prints as two
  different texts that read alike. The section does not try to detect lookalikes; the real labels stay
  in the data.
