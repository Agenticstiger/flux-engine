---
title: "Runs offline: your data and your model stay yours"
description: "FLUX needs no internet to produce results and has no LLM inside it. Where the network can be touched, how to keep it off, how an object store target treats instance metadata, and how CI checks it."
---

# Runs offline: your data and your model stay yours

FLUX is built for people who care where their data goes and which model reads it. This page says what the engine promises, every place the network can be touched, how to keep each one off, and how the project checks the promise.

## The promise

- FLUX needs no internet to produce results. `init`, `validate`, `plan`, `apply`, `run`, `verify`, `ship`, `replay`, `story`, `calibrate`, `forecast`, `explain`, `describe` and `plugins` are all local.
- No socket, HTTP client or LLM library is used outside tests, other than the places listed in the next section. Schema validation never fetches a remote reference.
- There is no telemetry and no update check.
- There is no LLM inside FLUX. Any AI help uses the model you choose, for example the one in your IDE. The [agent skills](https://github.com/Agenticstiger/flux-engine/blob/main/skills/README.md) are plain text that runs on that model, a local one included.
- Consumer queries (the `sql` extra) run DuckDB in a child process with external access, extension install and load, and community extensions all off, and its configuration locked.

## Every place the network can be touched

| Where | When | How to keep it off |
|---|---|---|
| A `--sink` to an object store: `s3://`, `gs://`, `az://` or `abfs://`, with the `parquet`, `arrow-ipc` or `delta` sink | Only when you name such a target | Write to a local directory instead, for example `--sink parquet:./out` |
| A `--sink adbc:...` database target | Only when you name a driver and a database that is not local | Use a local file such as `adbc:sqlite` or `adbc:duckdb`, or no database sink |
| An Iceberg catalogue, `--sink iceberg:...` | Only when your own pyiceberg configuration points it at a remote catalogue | Do not pass the `iceberg` sink, or configure a local catalogue |
| A Kafka sink, `--sink kafka:HOST:PORT[,...]` | Only when you name it: it contacts the brokers you name and, if you name one, the schema registry (`--sink-opt registry=URL`). With a credential over `SASL_PLAINTEXT` it also resolves the brokers' names, to check that each is this machine | Do not pass the `kafka` sink, or name brokers and a registry on this machine |
| `OpenLineageHttpEmitter` (the `lineage` extra) | Only when your own code builds it with a URL | Do not build it with a URL |

Nothing else touches the network. To be sure for a whole session, run FLUX where there is no network: `unshare --net` on Linux, or `docker run --network none`.

## Object stores and instance metadata

A cloud credential chain can end by asking the cloud's instance metadata service (`169.254.169.254` and its siblings) for the credentials of the machine it runs on. FLUX does not let that happen by surprise.

An S3, GCS or Azure target with no explicit credentials is refused before the run with `FLX-3505`. Nothing is contacted. The refusal is the same for the `parquet`, `arrow-ipc` and `delta` sinks.

Explicit credentials are the ones the store reads from your environment:

| Target | Counts as explicit credentials |
|---|---|
| `s3://` | `AWS_ACCESS_KEY_ID` with `AWS_SECRET_ACCESS_KEY`; or `AWS_WEB_IDENTITY_TOKEN_FILE` with `AWS_ROLE_ARN`; or `AWS_SKIP_SIGNATURE=true` for a public bucket |
| `gs://` | `GOOGLE_SERVICE_ACCOUNT`, `GOOGLE_SERVICE_ACCOUNT_PATH`, `GOOGLE_SERVICE_ACCOUNT_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_BEARER_TOKEN` or `GOOGLE_SKIP_SIGNATURE`; or the gcloud application default credentials file, `~/.config/gcloud/application_default_credentials.json` |
| `az://` and `abfs://` | An account key (`AZURE_STORAGE_ACCOUNT_KEY`), a SAS (`AZURE_STORAGE_SAS_KEY`) or `AZURE_STORAGE_TOKEN`; or a client id with a client secret and a tenant; or a client id with a tenant and `AZURE_FEDERATED_TOKEN_FILE`; or `AZURE_USE_AZURE_CLI=true`; or `AZURE_SKIP_SIGNATURE=true` |

Some cases to know:

- An AWS profile alone does not count for the `parquet` and `arrow-ipc` sinks, because the store does not read profiles. It counts for the `delta` sink.
- The container credential URIs of ECS and EKS count as instance identity, so they need the opt-in below.
- A sink takes no credential as an option (`FLX-3508`).

If you mean to use the credentials of the machine or workload itself (an EC2, ECS or pod role, an Azure managed identity, a GCE service account), say so with the opt-in:

```text
fluxsim run . --sink parquet:s3://my-bucket/simulations/run-a --sink-opt instance-identity=true
```

`instance-identity` is a true or false option, like `verify`. The `parquet`, `arrow-ipc` and `delta` sinks accept it. With it, the run behaves as it did before this rule. Without it and without explicit credentials, `fluxsim explain FLX-3505` prints the cause and the remedy: set explicit credentials in your environment, or pass the opt-in.

The `adbc` sink and Iceberg catalogues are configured by your own settings and are unchanged by this rule: FLUX hands the database driver and the catalogue loader only what you configured, and neither offers one setting that stops a client from asking a metadata service, so the rule does not cover them. To control what they contact, set it where they read their own configuration: the driver options of the `adbc` sink (`--sink-opt db.KEY=VALUE`), and the catalogue's configuration for `iceberg` (the `.pyiceberg.yaml` in your home directory or the `PYICEBERG_*` variables).

## Plugins

A generator plugin, declared in a bundle, is your own code. It runs with your privileges, and FLUX does not sandbox its network or its file access. A plugin is imported only when you allow it by name with `--allow-generator NAME`, and `FLX-3119` is the refusal when you have not.

So allow only plugins you trust, and run a bundle you did not write in a container with no network.

## AI help

There is no LLM inside FLUX, and the engine never sends a prompt or a row to a model. The [agent skills](https://github.com/Agenticstiger/flux-engine/blob/main/skills/README.md) in the `skills/` directory are plain text. Your agent reads them and runs the `fluxsim` commands on whichever model you chose, a local one included. They pre-approve nothing, so the agent asks before each command.

## How it is checked

Three checks run in CI:

1. Every pytest run CI makes blocks sockets except loopback and Unix sockets (the pytest-socket flags `--disable-socket --allow-hosts=127.0.0.1,::1,localhost --allow-unix-socket`). A connection to any other host fails. A test may opt out only with a reviewed entry, and none does today.
2. A CI job named "Offline journey" runs a real command line journey inside `sudo unshare --net`, with only loopback up: `fluxsim init` from a contract and a profile, `plan`, `run --verify --ship`, `story`, and a consumer query check. Every command must exit 0.
3. `tests/test_offline_guard.py` scans the source. No module imports a network or model client outside the reviewed places: the sinks and the OpenLineage emitter.

To run them yourself:

```text
make test-fast
scripts/offline_journey.sh
sudo unshare --net scripts/offline_journey.sh
```

`make test-fast` runs with the socket flags. `scripts/offline_journey.sh` runs the journey on any machine, and the last line runs it with no network on Linux.

## Limits

- The socket block covers the test process. The namespace job covers subprocesses and the whole program.
- The plugin flags block connections, not DNS lookups.
- A hostile plugin, or a library you install yourself, is outside what FLUX can promise.
- FLUX does not sandbox plugins. See above.

See also [the evidence pack, and what it does not claim](./the-evidence-pack.md) and [load a simulation into a database](../how-to/load-a-simulation-into-a-database.md).
