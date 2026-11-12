---
title: "Load a simulation into a database"
description: "Write a run's rows to Parquet or Arrow IPC files, or load them into SQLite, DuckDB, PostgreSQL, Snowflake or BigQuery, with fluxsim run --sink."
---

# Load a simulation into a database

Every run writes its rows to the run directory as NDJSON. Use this page to write the same rows, as typed columns, to Parquet or Arrow IPC files or into a database table, in the same `fluxsim run`.

The stream's `outputDigest` does not change when you add a sink. The engine computes it before a sink sees a row, so a sink decides where the rows land and nothing about what they are.

## Install what the sink needs

The engine installs without any columnar or database library. Each sink lives behind an extra, and the database sink also needs the ADBC driver for your database.

```text
pip install 'flux-sim-engine[arrow]'                        # Parquet and Arrow IPC files
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

`fluxsim plugins` lists the sinks and says whether what each one needs is installed. It imports none of them to find out.

```console
$ fluxsim plugins
vertical packs:
  telco
  towers
sinks (fluxsim run --sink NAME:TARGET):
  adbc       built-in  needs flux-sim-engine[adbc] (installed)
  arrow-ipc  built-in  needs flux-sim-engine[arrow] (installed)
  parquet    built-in  needs flux-sim-engine[arrow] (installed)
```

## Write Parquet files

Name the sink and its target with `--sink NAME:TARGET`. For a file sink the target is a directory, and there is no default: a sink never writes anywhere you did not name.

```console
$ fluxsim init demo
created demo with a small FLUX bundle and its FLUID contract (10 files):
...
$ cd demo
$ fluxsim run . --sink parquet:../parquet
plan written to .flux/runs/8dec55892096169f/plan.json (runId=8dec55892096169f, planDigest=sha256:8dec55892096169f5b6a9281a379f5dc9c085418ad2d22d52543a34835c26bb3)
applied to .flux/runs/8dec55892096169f (runDigest=sha256:c4bb981d99467e59f8a89c546d59a35fb35f973c3abe77d70b2af1174604915f)
emitted payment_recovery_moment: 154 rows (scenario)
delivered payment_recovery_moment: 154 rows in 1 parquet file under ../parquet/payment_recovery_moment/
run 8dec55892096169f complete
$ ls ../parquet/payment_recovery_moment
_manifest-d9e491cf29bf8c57-parquet.json
d9e491cf29bf8c57-00000.parquet
```

The file name starts with the first 16 hex characters of the stream's `outputDigest`. The same bundle and seed always write the same name, so running again replaces the file instead of adding a second copy, and a run with another seed lands beside it. The manifest lists every file of the stream with its SHA-256, size and row count, the stream header, and the writer settings, and its name says which sink wrote it. A stream is published whole or not at all: files are written under hidden names and renamed into place once every row is in, the manifest is written last, and a stream that fails part-way leaves no manifest and no files behind. Names that start with `_` or `.` are skipped by Parquet dataset readers, so the manifest is never read as data.

The columns are typed from the FLUID contract, and no value goes through a float:

| Logical type | Parquet / Arrow |
|---|---|
| `STRING`, `JSON` | string |
| `INT` | int64 |
| `DECIMAL(p,s)` | decimal, exactly `p` digits at scale `s` (up to 76 digits) |
| `BOOL` | boolean |
| `DATE` | date |
| `TIMESTAMP` | timestamp in milliseconds, UTC |
| `BYTES` | binary |

Files are compressed with zstd. Each file's metadata also carries the stream header, the run id, the `outputDigest` and `flux.synthetic = true`.

## Check that the files hold the run

Reading the files back and rendering the rows again gives the stream's `outputDigest`. The run's `receipt.json` records the digest the engine computed.

```python
import json
from pathlib import Path

from flux_engine.sinks.arrow import output_digest_of
from flux_engine.sinks.parquet import read_stream

work = Path(workdir)
receipt = json.loads((work / "demo/.flux/runs/8dec55892096169f/receipt.json").read_text())
expected = receipt["emits"]["payment_recovery_moment"]["outputDigest"]

table = read_stream(work / "parquet/payment_recovery_moment/_manifest-d9e491cf29bf8c57-parquet.json")
assert table.num_rows == 154
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
assert sum(count for _, count in markets) == 154
```

## Load into DuckDB or SQLite

For the database sink the target is the driver, and the connection is a sink option. SQLite and DuckDB take a database file.

```console
$ fluxsim run . --sink adbc:duckdb --sink-opt uri=../sim.duckdb
...
delivered payment_recovery_moment: 154 rows into sim_payment_recovery_moment at duckdb:../sim.duckdb
run 8dec55892096169f complete
```

The table is named after the expose with the prefix `sim_`, a namespace of its own so a simulated table cannot be mistaken for a real one. Change the prefix with `--sink-opt prefix=...`, or put the tables in a schema of their own with `--sink-opt schema=sim`, which is created when missing.

Run it again and the table still holds 154 rows. Each load first deletes the rows of its own `outputDigest`, then inserts the stream, in one transaction, so a replay never duplicates rows. A run with another seed has another digest and adds its rows beside the first.

```console
$ fluxsim run . --sink adbc:duckdb --sink-opt uri=../sim.duckdb --sink-opt verify=true
...
delivered payment_recovery_moment: 154 rows into sim_payment_recovery_moment at duckdb:../sim.duckdb
run 8dec55892096169f complete
```

With `verify=true` the sink reads its rows back inside the transaction and commits only when they digest to the stream's `outputDigest`.

```python
con = duckdb.connect(f"{workdir}/sim.duckdb", read_only=True)
assert con.execute("SELECT count(*) FROM sim_payment_recovery_moment").fetchone()[0] == 154
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
delivered payment_recovery_moment: 154 rows into twin_payment_recovery_moment at sqlite:../sim.sqlite
run 8dec55892096169f complete
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
export SIM_PG_URI='postgresql://sim:secret@db.example.com:5432/analytics'
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
$ grep -E '"(sink|table|target)"' .flux/runs/8dec55892096169f/sink-receipts.json
      "sink": "adbc",
      "table": "sim_payment_recovery_moment",
      "target": "duckdb:../sim.duckdb",
      "sink": "adbc",
      "table": "twin_payment_recovery_moment",
      "target": "sqlite:../sim.sqlite",
      "sink": "parquet",
      "target": "../parquet",
```

A receipt never holds a user, a password, a query string or a fragment: a target such as `postgresql://sim:secret@db.example.com/analytics?sslmode=require` is recorded as `postgresql://db.example.com/analytics`. With `--ship`, the sink runs after `verify` and before `ship`, so `ship` packs `sink-receipts.json` into the evidence archive with the rest of the run.

## When something is wrong

A mistake in the command line stops it before the simulation runs and names what to change: an unknown sink, a missing extra or driver, a missing target, an unknown or out-of-range setting, or a password on the command line.

```console
$ fluxsim run . --sink parquet  # exit 2
FLX-3504: a sink was given a target or an option it cannot use -- the parquet sink needs a directory: --sink parquet:DIRECTORY
...
$ fluxsim run . --sink adbc:postgresql --sink-opt uri=postgresql://sim:secret@db.example.com/analytics  # exit 2
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

Iceberg tables and Kafka topics are not sinks here. Write Parquet and load it with the tools you already use for those.
