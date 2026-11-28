# Sinks

Offline first: `fluxsim plugins` and `fluxsim apply --help`. The pages: [Load a simulation into a database](https://github.com/Agenticstiger/flux-engine/blob/v0.13.1/docs/how-to/load-a-simulation-into-a-database.md) and, for Kafka, [Stream a simulation into Kafka](https://github.com/Agenticstiger/flux-engine/blob/v0.13.1/docs/how-to/stream-a-simulation-into-kafka.md).

A sink writes the rows of a whole run as typed columns, in addition to the NDJSON the run directory already holds. `--sink NAME:TARGET` works on `apply` and `run`; `--sink-opt KEY=VALUE` is repeatable. A run writes to one sink per command; to deliver to a second place, run `apply` again with the other `--sink`. The stream's `outputDigest` is computed before a sink sees a row, so a sink changes where rows land and nothing about what they are. A share (`--shard`) is refused by `--sink`.

## What is installed

```bash
fluxsim plugins
fluxsim plugins --json
```

It lists each sink, the extra it needs and whether that is installed, without importing it. The Parquet and Arrow IPC sinks need the arrow extra of flux-sim-engine; the database sink needs the adbc extra plus the ADBC driver of the database (SQLite, DuckDB, PostgreSQL, Snowflake or BigQuery); the Kafka sink needs the kafka extra. A missing extra or driver is FLX-3501: ask the user to install it; do not install it yourself.

## `parquet:DIR` and `arrow-ipc:DIR`

The target is a directory and has no default: a sink never writes anywhere the user did not name. Files land under `DIR/EXPOSE/`, named from the first 16 hex characters of the stream's `outputDigest`, with a manifest (`_manifest-...json`) listing each file with its SHA-256, size and row count. A stream is published whole or not at all. Columns are typed from the contract, and no value goes through a float.

```bash
fluxsim apply bundle --run-id RUN_ID --sink parquet:OUT/parquet --sink-opt compression=zstd --sink-opt rows-per-file=100000
fluxsim apply bundle --run-id RUN_ID --sink arrow-ipc:OUT/arrow --sink-opt verify=true
```

| Setting | Sink | Meaning |
|---|---|---|
| `compression` | parquet: `zstd` (default), `snappy`, `gzip`, `lz4`, `brotli`, `none`; arrow-ipc: `zstd`, `lz4`, `none` | file compression |
| `compression-level` | parquet | the codec's level |
| `row-group-size` | parquet | rows per row group (default 1048576) |
| `rows-per-file` | parquet, arrow-ipc | start a new file after this many rows |
| `batch-size` | every sink | rows per Arrow batch (default 65536) |
| `verify` | every sink | read back what was written and check its digest before publishing |

## `adbc:DRIVER`

The target is the driver name: `sqlite`, `duckdb`, `postgresql`, `snowflake` or `bigquery`. The database to open reaches the sink only as `--sink-opt uri-env=VAR`, a variable the user sets to a file path or a URI; the file is one the user named. Run a database sink only when the user asked for it and named the destination. Tables are named `sim_` plus the expose name and carry four columns of the sink's own (`_flux_synthetic`, `_flux_run_id`, `_flux_output_digest`, `_flux_row`). Each load deletes the rows of its own `outputDigest` and inserts the stream in one transaction, so a replay never duplicates rows.

| Setting | Meaning |
|---|---|
| `uri-env` | the name of an environment variable that holds the URI |
| `schema` | the schema (BigQuery: dataset) to write into |
| `prefix` | table name prefix, default `sim_`, may be empty |
| `mode` | `append` (default: the stream's slice is replaced), `create` (refuse an existing table) or `replace` (drop and create) |
| `db.KEY` | an ADBC database option for the driver |
| `db.KEY-env` | the name of an environment variable that holds that driver option |

The sink touches only tables it created; a table without its four `_flux_` columns is refused in every mode (FLX-3506). Ask the user before using `mode=replace` against a database.

## `kafka:HOST:PORT[,HOST:PORT...]`

The target names the brokers, and a run contacts only those and the schema registry the user names. Records are keyed by entity, with event time in the record timestamp, and delivery is exactly-once. It needs the kafka extra. Credentials come only from environment variables the user sets, as for a database; never put one in a command. Run it only when the user asked for it and named the brokers. The options and the audience gate are on the page linked above.

## The credential rule

An object store target (`s3://`, `gs://`, `az://`, `abfs://`) with no credentials is refused unless you pass `--sink-opt instance-identity=true`.


A command line is visible to every user of the machine and kept in shell history, so a secret never goes in one.

- For a server database, do not write a URI that holds a password. Ask the user to put the URI in an environment variable they set themselves, and pass only its name: `--sink-opt uri-env=VAR`.
- For a driver option that names a secret (a token, a password), the user sets an environment variable and you pass `--sink-opt db.KEY-env=VAR`.
- Never set, print, echo or read the variable, and never put its value in a file, a command or a reply. A URI with a password or a secret query parameter is refused (FLX-3504); do not retry it with the secret hidden some other way.
- Receipts never hold a user, a password, a query string or a fragment of a target.

A server example, for the user to run once the variable is set: `fluxsim apply bundle --run-id RUN_ID --sink adbc:postgresql --sink-opt uri-env=SIM_PG_URI --sink-opt schema=sim`.

## Errors

| Code | Meaning |
|---|---|
| FLX-3501 | the sink's extra or driver is not installed |
| FLX-3504 | a target or a setting is missing or wrong, or a secret was on the command line |
| FLX-3505 | writing failed part-way; nothing partial was left |
| FLX-3506 | the database sink will not touch a table it did not create |
| FLX-3507 | there is no sink by that name |

Run `fluxsim explain FLX-nnnn` for the remedy. Each delivery is recorded in `sink-receipts.json` in the run directory, and with `run --ship` the sink runs after verify and before ship, so the pack carries those receipts.
