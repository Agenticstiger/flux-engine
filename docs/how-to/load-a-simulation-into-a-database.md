---
title: "Load a simulation into a database"
description: "Write a run's rows to Parquet or Arrow IPC files, locally or in an object store, to Iceberg or Delta Lake tables, or load them into SQLite, DuckDB, PostgreSQL, Snowflake or BigQuery, with fluxsim run --sink."
---

# Load a simulation into a database

Every run writes its rows to the run directory as NDJSON. Use this page to write the same rows, as typed columns, to Parquet or Arrow IPC files or into a database table, in the same `fluxsim run`. `fluxsim apply` takes the same `--sink` and `--sink-opt`, and with `--run-id` it writes the sink from the run directory's own files after checking each stream against the receipt.

The stream's `outputDigest` does not change when you add a sink. The engine computes it before a sink sees a row, so a sink decides where the rows land and nothing about what they are.

## Install what the sink needs

The engine installs without any columnar or database library. Each sink lives behind an extra, and the database sink also needs the ADBC driver for your database.

```text
pip install 'flux-sim-engine[arrow]'                        # Parquet and Arrow IPC files
pip install 'flux-sim-engine[publish]'                      # the same files in an object store, Iceberg and Delta tables
pip install 'flux-sim-engine[adbc]' duckdb                  # DuckDB
pip install 'flux-sim-engine[adbc]' adbc-driver-postgresql  # PostgreSQL
```

| Database | Driver package |
|---|---|
| SQLite | `adbc-driver-sqlite` |
| DuckDB | `duckdb`, 1.5 or later (it ships its own driver) |
| PostgreSQL | `adbc-driver-postgresql` |
| Snowflake | `adbc-driver-snowflake` |
| BigQuery | `adbc-driver-bigquery` |

`fluxsim plugins` lists the sinks and says whether what each one needs is installed. It imports none of them to find out. It lists the generator plugins the same way, and none are installed here. A generator plugin is your own code: it runs with your privileges and FLUX does not sandbox it, so allow only plugins you trust ([Runs offline](../explanation/offline.md#plugins)).

```console
$ fluxsim plugins
vertical packs:
  telco
  towers
sinks (fluxsim run --sink NAME:TARGET):
  adbc       built-in  needs flux-sim-engine[adbc] (installed)
  arrow-ipc  built-in  needs flux-sim-engine[arrow] (installed)
  delta      built-in  needs flux-sim-engine[publish] (...)
  iceberg    built-in  needs flux-sim-engine[publish] (...)
  parquet    built-in  needs flux-sim-engine[arrow] (installed)
generators (entry points of flux_engine.generators, none imported):
  none installed
```

## Write Parquet files

Name the sink and its target with `--sink NAME:TARGET`. For a file sink the target is a directory, and there is no default: a sink never writes anywhere you did not name.

```console
$ fluxsim init demo
created demo with a small FLUX bundle and its FLUID contract (10 files):
...
$ cd demo
$ fluxsim run . --sink parquet:../parquet
plan written to .flux/runs/205ba6e504c40820/plan.json (runId=205ba6e504c40820, planDigest=sha256:205ba6e504c408208fb15c5da5f96edac2f514606d6024bc19733c6c5cac90cc)
applied to .flux/runs/205ba6e504c40820 (runDigest=sha256:6c9de0409b3c2e98c435c3141f1728ff17511087cd3b39f1e87625fa5d238f06)
emitted payment_recovery_moment: 155 rows (scenario)
delivered payment_recovery_moment: 155 rows in 1 parquet file under ../parquet/payment_recovery_moment/
run 205ba6e504c40820 complete
$ ls ../parquet/payment_recovery_moment
_manifest-db1a89eefb51c42e-parquet.json
db1a89eefb51c42e-00000.parquet
```

The file name starts with the first 16 hex characters of the stream's `outputDigest`. The same bundle and seed always write the same name, so running again replaces the file instead of adding a second copy, and a run with another seed lands beside it. The manifest lists every file of the stream with its SHA-256, size and row count, the stream header, and the writer settings, and its name says which sink wrote it. A stream is published whole or not at all: files are written under hidden names and renamed into place once every row is in, the manifest is written last, and a stream that fails part-way to a local directory leaves no manifest and no files behind. On an object store, objects uploaded before a failure can stay behind without a manifest, and a stream is whole only once its manifest is there. Names that start with `_` or `.` are skipped by Parquet dataset readers, so the manifest is never read as data.

The columns are typed from the FLUID contract, and no value goes through a float:

| Logical type | Parquet / Arrow |
|---|---|
| `STRING`, `JSON` | string |
| `INT` | int64, or the width the contract names: `tinyint` int8, `smallint` int16, `integer` and `mediumint` int32, `bigint` int64 |
| `DECIMAL(p,s)` | decimal, exactly `p` digits at scale `s` (up to 76 digits) |
| `BOOL` | boolean |
| `DATE` | date |
| `TIMESTAMP` | timestamp in milliseconds, UTC |
| `BYTES` | binary |

Files are compressed with zstd. Each file's metadata also carries the stream header, the run id, the `outputDigest` and `flux.synthetic = true`. The receipt names the Parquet writer and its version (`parquet-cpp-arrow version N`) beside the library's. The width of an integer column changes the file and not the stream: the digest is over the rendered rows.

## Write to an object store

The Parquet and Arrow IPC sinks also take an object store URL as their target, with `s3://`, `gs://`, `az://` or `abfs://` as the scheme. The URL's host is the bucket (or container) and its path is the prefix the files go under. This needs the `[publish]` extra, which installs obstore, and without it the sink refuses with the install line.

```text
fluxsim run . --sink parquet:s3://my-bucket/simulations/run-a
fluxsim run . --sink arrow-ipc:gs://my-bucket/simulations/run-a
```

Each object has the key `<prefix>/<exposeId>/<name>`, where `<name>` is the file name a local target would write, and the manifest is uploaded last, as the commit marker. The bytes are the ones a local target writes, and the receipt records the target without credentials, each object's key, size and SHA-256, and the writer as `obstore` with its version.

A target with no explicit credentials is refused before the run with `FLX-3505`, and nothing is contacted, unless you pass `--sink-opt instance-identity=true`. That option opts in to the credentials of the machine or workload itself (an EC2, ECS or pod role, an Azure managed identity, a GCE service account), which the store asks the cloud's instance metadata service for. Explicit credentials are the variables the store reads from your environment, for example `AWS_ACCESS_KEY_ID` with `AWS_SECRET_ACCESS_KEY`. [Runs offline](../explanation/offline.md) lists them for each cloud.

Credentials and endpoints come only from your own environment or from the platform's credential chain, as obstore reads them: the `AWS_*`, `GOOGLE_*` or `AZURE_*` variables, a profile, an instance or workload identity. No `--sink-opt` reaches the store, a target that carries user information, a query or a fragment (`s3://user:pass@bucket/`) is refused, and an error from the store is reported with URLs' user information and any credential variable's value removed.

The path is split on `/` and each segment must be a plain name. An empty segment, `.`, `..`, a backslash and a percent-encoded `/`, `\` or `.` (`%2F`, `%5C`, `%2E`) are refused with `FLX-3510`, so no spelling of a target can write outside the prefix it shows. A trailing `/` is dropped, and a target must name a prefix.

## Write Iceberg and Delta tables

The `iceberg` and `delta` sinks write each stream as a table, so a query engine reads it as one. Both need the `[publish]` extra, which installs pyiceberg and deltalake, and without it the sink refuses with the install line. Each writes one table per stream, named by the stream's `exposeId`. A table sink holds the stream's rows until the whole stream has arrived, writes them as one table write, and writes nothing for a stream that stopped short.

Both take one option, `mode`, and no other: nothing passes through to pyiceberg or deltalake. A failed write is reported as `FLX-3505` with the writer's error class and message, with URLs' user information and any credential variable's value removed.

### Iceberg

```text
fluxsim run . --sink iceberg:prod/simulations
fluxsim run . --sink iceberg:prod/simulations/run-a --sink-opt mode=append
```

The target is `iceberg:<catalog>/<namespace>[/<level>...]`: the catalogue's name as pyiceberg's configuration knows it, then the levels of the namespace, at least one. The levels are separated by `/`, never by a dot. The catalogue name and each level are checked as a prefix's segments are (`FLX-3510`): no empty segment, `.`, `..`, backslash or percent-encoded separator. The namespace must exist already; the sink does not create one.

The table is named by the tuple `(*levels, exposeId)`, so an `exposeId` that holds a dot stays one table name and never becomes a level.

| `mode` | Writes |
|---|---|
| `create` (the default) | Creates the table from the stream's Arrow schema and writes the rows in the same commit. A table that exists is refused. |
| `append` | Adds the rows to a table that exists. An absent table is refused, since pyiceberg has no append that creates one. |
| `overwrite` | Replaces the contents of a table that exists. An absent table is refused. |

The catalogue is loaded with `pyiceberg.catalog.load_catalog(<catalog name>)` and no property, so its type, endpoint and credentials come only from your own pyiceberg configuration: a `.pyiceberg.yaml` in your home directory or in the directory `PYICEBERG_HOME` names, and `PYICEBERG_*` environment variables. A bundle cannot choose them.

pyiceberg also reads a `.pyiceberg.yaml` from the current directory, which could point the catalogue, and the storage requests it signs with your credentials, at another endpoint. So the sink refuses to start (`FLX-3511`) when the current directory holds one and is neither your home directory nor `PYICEBERG_HOME`. Run the command from another directory, or move the file to one of those two.

The receipt records the target as `iceberg:<catalog>/<namespace>`, with no URI and no property, the table identifier as a list, the id of the snapshot the write made (as text, since it is a 64-bit integer), and the writer as `pyiceberg` with its version.

### Delta Lake

```text
fluxsim run . --sink delta:s3://my-bucket/simulations/run-a
fluxsim run . --sink delta:../tables --sink-opt mode=overwrite
```

The target is an object store URL (`s3://`, `gs://`, `az://` or `abfs://`), checked as the file sinks' object targets are (`FLX-3510`), or a local directory. A local directory is resolved first, and a directory that is a symbolic link is refused (`FLX-2118`). Each stream's table is at `<target>/<exposeId>`.

| `mode` | Writes |
|---|---|
| `error` (the default) | Creates the table. A table that exists is refused. |
| `append` | Adds the rows to the table, creating it when it is absent. |
| `overwrite` | Replaces the contents of the table. |

The table is written with `deltalake.write_deltalake` and no `storage_options`, so credentials and endpoints come only from your own environment or the platform's credential chain, as deltalake reads them (an object store target is refused with `FLX-3505` unless it has explicit credentials or you pass `--sink-opt instance-identity=true`, as for the file sinks): the `AWS_*`, `GOOGLE_*` or `AZURE_*` variables, a profile, an instance or workload identity. A target that carries user information, a query or a fragment is refused.

The receipt records the target without credentials, the table's location, the table version the write made, and the writer as `deltalake` with its version.

## Check that the files hold the run

Reading the files back and rendering the rows again gives the stream's `outputDigest`. The run's `receipt.json` records the digest the engine computed.

```python
import json
from pathlib import Path

from flux_engine.sinks.arrow import output_digest_of
from flux_engine.sinks.parquet import read_stream

work = Path(workdir)
receipt = json.loads((work / "demo/.flux/runs/205ba6e504c40820/receipt.json").read_text())
expected = receipt["emits"]["payment_recovery_moment"]["outputDigest"]

table = read_stream(work / "parquet/payment_recovery_moment/_manifest-db1a89eefb51c42e-parquet.json")
assert table.num_rows == 155
assert output_digest_of(table).value == expected
```

Add `--sink-opt verify=true` to have the sink make the same check on its own files before it publishes them.

## Query them

Any Parquet reader works. With DuckDB:

```python
import duckdb

markets = duckdb.connect().execute(
    f"SELECT market, count(*) FROM '{workdir}/parquet/payment_recovery_moment/*.parquet' "
    "GROUP BY market ORDER BY market"
).fetchall()
assert sum(count for _, count in markets) == 155
```

## Load into DuckDB or SQLite

For the database sink the target is the driver, and the connection is a sink option. SQLite and DuckDB take a database file.

```console
$ fluxsim run . --sink adbc:duckdb --sink-opt uri=../sim.duckdb
...
delivered payment_recovery_moment: 155 rows into sim_payment_recovery_moment at duckdb target sha256:41d91f404486
run 205ba6e504c40820 complete
```

The table is named after the expose with the prefix `sim_`, a namespace of its own so a simulated table cannot be mistaken for a real one. Change the prefix with `--sink-opt prefix=...`, or put the tables in a schema of their own with `--sink-opt schema=sim`, which is created when missing.

Run it again and the table still holds 155 rows. Each load first deletes the rows of its own `outputDigest`, then inserts the stream, in one transaction, so a replay never duplicates rows. A run with another seed has another digest and adds its rows beside the first.

```console
$ fluxsim run . --sink adbc:duckdb --sink-opt uri=../sim.duckdb --sink-opt verify=true
...
delivered payment_recovery_moment: 155 rows into sim_payment_recovery_moment at duckdb target sha256:41d91f404486
run 205ba6e504c40820 complete
```

With `verify=true` the sink reads its rows back inside the transaction and commits only when they digest to the stream's `outputDigest`.

```python
con = duckdb.connect(f"{workdir}/sim.duckdb", read_only=True)
assert con.execute("SELECT count(*) FROM sim_payment_recovery_moment").fetchone()[0] == 155
columns = [row[0] for row in con.execute("DESCRIBE sim_payment_recovery_moment").fetchall()]
assert columns[-4:] == ["_flux_synthetic", "_flux_run_id", "_flux_output_digest", "_flux_row"]
label = con.execute(
    "SELECT comment FROM duckdb_tables() WHERE table_name = 'sim_payment_recovery_moment'"
).fetchone()[0]
assert json.loads(label)["synthetic"] is True
con.close()
```

After the stream's own columns, every table has four of the sink's:

- `_flux_synthetic`: always true, on every row.
- `_flux_run_id`: the run the row came from.
- `_flux_output_digest`: the stream the row belongs to. A slice is the rows with one digest.
- `_flux_row`: the row's place in the stream. `ORDER BY _flux_row` returns the stream in its own order.

Where the database has table comments (DuckDB, PostgreSQL, Snowflake) or descriptions (BigQuery), the table is also labelled with a JSON comment that says `"synthetic": true` and names the expose, the run and the latest load's digest. SQLite has neither, so there the four columns are the label.

SQLite has no exact decimal type, and its driver would write a timestamp without its `Z`. On SQLite the sink stores `DECIMAL` and `TIMESTAMP` values as the text the engine renders, such as `6998.9770` and `2026-07-01T12:00:00.125Z`, which SQLite's date functions read. A `DECIMAL` wider than 38 digits goes to every database as text, since 38 digits is the widest decimal most of them store exactly.

```console
$ fluxsim run . --sink adbc:sqlite --sink-opt uri=../sim.sqlite --sink-opt prefix=twin_
...
delivered payment_recovery_moment: 155 rows into twin_payment_recovery_moment at sqlite target sha256:ec2cb5e2e8a1
run 205ba6e504c40820 complete
```

### Modes

`--sink-opt mode=` picks what happens to a table that already exists:

| Mode | Table missing | Table exists |
|---|---|---|
| `append` (default) | created | the stream's slice is replaced, other slices are kept |
| `create` | created | refused |
| `replace` | created | dropped and created again, with the stream alone |

The sink only ever changes tables it created. A table without its four `_flux_` columns is refused in every mode, so a real table that happens to share a name is never appended to, emptied or dropped (`FLX-3506`). In `append` mode the columns must also match the stream's; when the contract changed, use `mode=replace`.

## PostgreSQL, Snowflake and BigQuery

A server database's URI usually holds a password, and a command line is visible to every user of the machine and kept in shell history. Put the URI in an environment variable and pass its name with `uri-env`. A `uri=` with a password or a secret query parameter is refused.

```text
export SIM_PG_URI='postgresql://sim:pa55w0rd@db.example.com:5432/analytics'
fluxsim run . --sink adbc:postgresql --sink-opt uri-env=SIM_PG_URI --sink-opt schema=sim
```

PostgreSQL loads through `COPY` in binary. Its driver creates a `DECIMAL` column as an unconstrained `NUMERIC`, which keeps the number exactly and drops trailing zeros.

```text
export SIM_SNOWFLAKE_URI='sim_loader:secret@my-account/ANALYTICS/SIM?warehouse=LOAD_WH'
fluxsim run . --sink adbc:snowflake --sink-opt uri-env=SIM_SNOWFLAKE_URI
```

Snowflake loads through a stage and `COPY INTO`.

BigQuery takes driver options instead of a URI. Any `--sink-opt db.KEY=VALUE` goes to the driver as an ADBC database option, and by default the driver authenticates with your Google application default credentials. A driver option that names a secret, such as a token or a password, is refused inline; pass `--sink-opt db.KEY-env=VAR` to read it from the environment.

```text
fluxsim run . --sink adbc:bigquery \
  --sink-opt db.adbc.bigquery.sql.project_id=my-project \
  --sink-opt db.adbc.bigquery.sql.dataset_id=sim
```

The test suite runs the database sink against SQLite and DuckDB on every change, and against PostgreSQL in a container when Docker is available. Snowflake and BigQuery are not exercised by the test suite: the sink drives them through their own ADBC drivers the same way, and a report from a run against either is welcome. Snowflake commits DDL on its own and BigQuery has no transaction here, so on those two a failed first load can leave an empty table, which the next run replaces.

## Settings

Pass each setting as `--sink-opt KEY=VALUE`, as many times as you need. An unknown key, or a value of the wrong type or out of range, stops the command before the simulation runs, and the message lists the keys the sink takes. A run writes to one sink: to deliver it to a second place, run again with the other `--sink`. The run is the same, so nothing is simulated differently, and its receipt is added beside the first.

Credentials come from the environment or the platform's own credential chain, never from a setting. A `--sink-opt` whose name names a credential (a password, a secret, a token, a signature, or a key such as `access-key`) is refused before the simulation runs, and so is a target that carries user information, a query string or a fragment. A setting whose name ends in `-env`, such as `uri-env`, holds only the name of an environment variable, so it is allowed.

| Sink | Setting | Default | Meaning |
|---|---|---|---|
| `parquet` | `compression` | `zstd` | `zstd`, `snappy`, `gzip`, `lz4`, `brotli` or `none` |
| `parquet` | `compression-level` | `3` for zstd, the codec's own otherwise | the codec's level, where it has one; checked against the codec's range |
| `parquet` | `row-group-size` | `1048576` | rows per row group |
| `parquet`, `arrow-ipc` | `rows-per-file` | one file | start a new file after this many rows |
| `arrow-ipc` | `compression` | `zstd` | `zstd`, `lz4` or `none` |
| every sink | `batch-size` | `65536` | rows per Arrow batch (for `arrow-ipc`, per record batch in the file) |
| every sink | `verify` | `false` | read what was written back and check its digest before publishing |
| `adbc` | `uri` | none | the database file, or a URI without a password |
| `adbc` | `uri-env` | none | the name of an environment variable holding the URI |
| `adbc` | `schema` | the default | the schema (BigQuery: dataset) to write into |
| `adbc` | `prefix` | `sim_` | the table name prefix; may be empty |
| `adbc` | `mode` | `append` | `append`, `create` or `replace` |
| `adbc` | `db.KEY` | none | an ADBC database option for the driver |
| `adbc` | `db.KEY-env` | none | the name of an environment variable holding that driver option; required for one that names a secret |

## Receipts and the evidence pack

Each sink returns a receipt: the rows, and either the files with their SHA-256 or the table. `fluxsim run` records each one in `sink-receipts.json` in the run directory as soon as its stream is published, so a delivery that fails on a later stream still leaves the earlier ones on record. A receipt is kept per sink, target, table and expose: delivering the run to another place adds one, and delivering it to the same place again replaces that one.

```console
$ grep -E '"(sink|table|target)"' .flux/runs/205ba6e504c40820/sink-receipts.json
      "sink": "adbc",
      "table": "sim_payment_recovery_moment",
      "target": "duckdb target sha256:41d91f404486",
      "sink": "adbc",
      "table": "twin_payment_recovery_moment",
      "target": "sqlite target sha256:ec2cb5e2e8a1",
      "sink": "parquet",
      "target": "../parquet",
```

A receipt and every line the command prints hold a label of the target, never the target as typed. A URL keeps its scheme, host, port and path and shows its user, password, query string and fragment as `…`: `postgresql://sim:pa55w0rd@db.example.com/analytics?sslmode=require` is recorded as `postgresql://…@db.example.com/analytics?…`. A `key=value` connection string keeps its `host`, `dbname` and similar pairs. Any other `uri`, a database file among them, is recorded as the driver and a short digest of the text (`duckdb target sha256:41d91f404486`), so two runs against the same file carry the same label and no secret typed in the wrong place is echoed. A directory of a Parquet or Arrow IPC sink is shown as typed. With `--ship`, the sink runs after `verify` and before `ship`, so `ship` packs `sink-receipts.json` into the evidence archive with the rest of the run.

## When something is wrong

A mistake in the command line stops it before the simulation runs and names what to change: an unknown sink, a missing extra or driver, a missing target, an unknown or out-of-range setting, or a password on the command line.

```console
$ fluxsim run . --sink parquet  # exit 2
FLX-3504: a sink was given a target or an option it cannot use -- the parquet sink needs a directory: --sink parquet:DIRECTORY
...
$ fluxsim run . --sink adbc:postgresql --sink-opt uri=postgresql://sim:pa55w0rd@db.example.com/analytics  # exit 2
FLX-3504: a sink was given a target or an option it cannot use -- the uri carries a password or a secret, and a command line is not the place for one: put the uri in an environment variable and pass --sink-opt uri-env=NAME
...
```

What only the run's own streams or the destination can tell stops the delivery after the run instead, with exit status 1: two exposes whose tables would be one (checked before any stream is written), a table the sink will not touch, or a write the destination refuses. The run directory is written, a stream that failed is left out whole, and `--ship` does not run.

| Code | What happened |
|---|---|
| [`FLX-3501`](../reference/errors/FLX-3501.md) | the sink's extra or driver is not installed; the message has the `pip install` line |
| [`FLX-3504`](../reference/errors/FLX-3504.md) | a target or a setting is missing or wrong |
| [`FLX-3505`](../reference/errors/FLX-3505.md) | writing failed part-way; nothing partial was left |
| [`FLX-3506`](../reference/errors/FLX-3506.md) | the database sink will not touch a table it did not create |
| [`FLX-3507`](../reference/errors/FLX-3507.md) | there is no sink by that name |

## Write your own sink

A package adds a sink without changing the engine, through the `flux_engine.sinks` entry-point group:

```text
[project.entry-points."flux_engine.sinks"]
mysink = "my_package.sink:target"
```

`my_package.sink.target(target, options)` receives the text after `mysink:` and the `--sink-opt` settings as strings. It checks them and returns an object with a `name` and a `sink_for(context)` method. `context` is a `flux_engine.sinks.receipts.StreamContext`: the run id, the expose id, the row count and the `outputDigest`. Each sink it returns implements `flux_engine.api.emit.EmitSink` (`open`, `write`, `close`). When `close` has seen every row, it sets `receipt` to a `flux_engine.sinks.receipts.SinkReceipt`; otherwise it leaves `receipt` as `None` and discards what it wrote. The engine loads the entry point only when you name the sink. A built-in name always wins over a package's.

These sink types are new and are not yet part of the governed `flux_engine.api` surface, so they may still change before they join it.

Kafka topics are not a sink here. Write Parquet and load it with the tools you already use for that.
