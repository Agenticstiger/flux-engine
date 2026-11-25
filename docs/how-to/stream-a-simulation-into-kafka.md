---
title: "Stream a simulation into Kafka"
description: "Send a run's streams to Kafka topics with fluxsim run --sink kafka: keyed by entity, with event time in the record timestamp, a history's changes as Debezium records, registry schemas, exactly-once delivery, optional pacing, and a gate on who may read them."
---

# Stream a simulation into Kafka

Use this page to put a run's streams on Kafka in the shape Kafka consumers expect: records keyed by their entity, event time in the record timestamp, a history's changes as change-data-capture records, values in the registry's schemas, and no duplicates when a delivery is retried. `fluxsim apply` takes the same `--sink` and `--sink-opt`, and with `--run-id` it sends the streams from the run directory's own files after checking each one against the receipt.

The stream's `outputDigest` does not change when you add the sink. The engine computes it before a sink sees a row, so the sink decides where the rows go and how fast, and nothing about what they are. The design is [ADR 0038](../adr/0038-a-kafka-sink-keys-records-by-entity-delivers-each-stream-exactly-once-and-sends-only-what-its-audience-may-see.md).

## Install what the sink needs

The sink lives behind the `[kafka]` extra, which installs confluent-kafka (the Python client over librdkafka) and its schema registry serializers. Without it the sink refuses with `FLX-3501` and this install line.

```text
pip install 'flux-sim-engine[kafka]'
```

## Start a local broker and registry

Any Kafka 4 cluster and any registry with Confluent's REST API will do. For a trial on one machine, a single broker and a Karapace registry:

```text
docker network create flux-kafka
docker run -d --name kafka --network flux-kafka -p 9092:9092 apache/kafka:4.4.0
docker run -d --name karapace --network flux-kafka -p 8081:8081 \
  -e KARAPACE_BOOTSTRAP_URI=kafka:19092 -e KARAPACE_PORT=8081 -e KARAPACE_HOST=0.0.0.0 \
  ghcr.io/aiven-open/karapace:6.2.4 python3 -m karapace
```

Check the broker's advertised listeners and the registry's bootstrap address against your own network; the lines above are a sketch for one laptop, not a deployment. Nothing on this page starts either of them for you.

## Run a bundle into Kafka

The examples use a device-telemetry bundle like the one in [Project a history](./project-a-history.md): a history `device_version` (natural entity `device_id`) under a parent `site`, its snapshot `device_current`, its change events `device_change` (columns `event_id`, `device_id`, `op`, `changed_at`, `recorded_at`, `before_firmware`, `after_firmware`), its periodic rows `device_month`, and a table `reading`. Name the sink and the bootstrap servers with `--sink kafka:HOST:PORT[,HOST:PORT...]`, and say who will read the topics:

```text
fluxsim run . --profile ../devices.profile.json --sink kafka:localhost:9092 --sink-opt audience=internal
```

The bundle must pin a privacy profile (or declare a generator plugin), or the run has no grade and the sink refuses every stream (see the audience gate below). The Simulation pins it under its extension block:

```text
extensions:
  com.agentics.flux/1:
    profile:
      digest: sha256:7716a97db17142ffc32bb0f76a69f8906aea29cf844a801eed1c6d078a170298
```

Replace the digest with the one of your own profile. The profile file itself is passed with `--profile` and lives outside the bundle, so a run that pins a profile needs `--profile PATH` too (without it the run stops with `FLX-3109`). The target is the bootstrap servers and nothing else. The sink connects to those brokers and to the registry you name with `registry`, and to nothing else: an address, a topic or a credential written in the contract is never contacted.

Everything that needs no network is checked first: the options when the target is opened, before the run, and then the audience gate and each stream's declarations before any row is sent. A refusal at that point has written nothing.

## Choose the audience

Every Kafka target declares who will read it. There is no default, and a target without `audience` is refused with `FLX-3504`.

| `audience` | Needs a run graded at least | Because |
|---|---|---|
| `internal` | `P1` | the readers work for the data's owner |
| `external` | `P3` | the readers do not, so the numbers must rest on differentially private marginals |

The grades compare in the order of [ADR 0020](../adr/0020-privacy-grades.md): `P5` is below `P1`, which is below `P3`, which is below `P4` and `P0`. A run whose generator plugin states `P5` reaches neither. The engine works the grade out as `apply` reads it, from the run's plan after its binding is checked, with every generator plugin the bundle declares, and hands it to the sink with each stream. Before it does, it checks that the run's `receipt.json` and streams belong to that plan and that the bundle is the plan's. A stream below its audience's minimum, or one the run has no grade for, is refused with `FLX-3512` before anything is contacted. A run that pins no profile and declares no generator plugin has no grade, so it goes to no Kafka target. The message names the grade, the audience and its minimum, and never a value of the data.

An external target is held to a grade worked out from the pinned profile itself, so it needs `--profile PATH`, the profile the Simulation pins, on `run` and on `apply --run-id`. Without it, or with another profile, the stream is refused with `FLX-3512` and the message names `--profile`. `apply` reads `--profile` with `--run-id` only together with `--sink`. The receipt records that grade as `profileGrade`.

```text
fluxsim apply . --run-id RUN_ID --profile ../devices.profile.json --sink kafka:broker-1.example.org:9093 --sink-opt audience=external
```

What the gate trusts is the same as in [ADR 0038](../adr/0038-a-kafka-sink-keys-records-by-entity-delivers-each-stream-exactly-once-and-sends-only-what-its-audience-may-see.md). An internal target trusts the run's bound record: the plan digest, `run-digest.json`, and the receipt and its streams bound to them. An external target is re-derived from the pinned profile. Someone who can rewrite every file of a run directory consistently can still make a run look like another one; for an external target that still needs the pinned profile to grade `P3`.

## Topics and keys

A stream's topic is a prefix and a name. The name is `topic.EXPOSE=NAME`, else the topic the contract binds the expose to when it is bound to Kafka, else the expose id. The prefix is `topic-prefix`, `sim.` by default, and it may not be empty, so a synthetic topic never has the name of the topic the contract declares. With the defaults the bundle's streams go to `sim.device_change`, `sim.device_current` and so on. Two streams of one delivery may not share a topic, and the name must be one Kafka allows (`FLX-3513`).

```text
fluxsim run . --profile ../devices.profile.json --sink kafka:localhost:9092 --sink-opt audience=internal \
  --sink-opt topic-prefix=synthetic. --sink-opt topic.device_change=changes
```

A missing topic is created with the broker's default partitions and replication, or with `partitions` and `replication-factor` when you give them. A snapshot's topic is created with `cleanup.policy=compact`. A topic that exists is checked first (`FLX-3514`): its `message.timestamp.type` must be `CreateTime`, since `LogAppendTime` would replace event time with the time of the write, and a snapshot's topic must be compacted.

Every record is keyed by its entity, so one entity's records keep their order in one partition. The key of a history's emit and of each of its projections is the history's natural entity; the key of any other expose is its primary entity. `key.EXPOSE=COLUMN[,COLUMN...]` names the columns instead, and an expose with neither is refused with `FLX-3513` naming the option.

| `key-format` | Key bytes |
|---|---|
| `string` (the default) | a one-column key as its rendered text, as Kafka's `StringSerializer` writes it |
| `json` | the key columns as one compact JSON object, in the order `key.EXPOSE` names them, as Debezium's JSON converter writes a key |

A key of several columns needs `json`. A null key value is refused (`FLX-3513`), naming the column and the row's position. The producer uses librdkafka's `murmur2_random` partitioner, so a record lands on the partition a Java producer would choose for the same key bytes and partition count.

## Event time and the broker's limits

The record timestamp is the event's time, set explicitly, in milliseconds since 1970 in UTC (a DATE at its midnight).

| Stream | Timestamp |
|---|---|
| a history's emit | the version's validity start |
| change events | the `date` column |
| snapshot | the validity start of the entity's last version when the snapshot carries it, else the history's `until` |
| periodic rows | the period column |
| any other expose | `timestamp.EXPOSE=COLUMN`, else the contract's `defaultAggTimeDimension`, else its one time dimension |

An expose with no timestamp column is refused with `FLX-3513`, naming `timestamp.EXPOSE`.

A timestamp at or before 1970-01-01T00:00:00Z is refused with `FLX-3514` (a broker refuses a negative one and a producer reads 0 as the current time); run a window that starts after it. A broker also refuses, from Kafka 4.0, a timestamp more than an hour ahead of its clock unless the topic says otherwise, through `message.timestamp.before.max.ms` and `message.timestamp.after.max.ms`. The sink checks every record against both limits before it sends it, and a record outside them stops the stream with `FLX-3514`. The message names the limit and the remedy: a topic configured for a backfill, or a run window nearer the present. The sink never moves a record's time to fit.

```text
kafka-configs.sh --bootstrap-server localhost:9092 --alter --entity-type topics \
  --entity-name sim.device_change \
  --add-config message.timestamp.before.max.ms=315360000000
```

## Change events as Debezium records

A history's change events are written in Debezium's envelope, so a consumer that reads a connector's topic reads these.

- `op` is the event's `op` column: `c`, `u` or `d`. An `events` entry without `op` is refused (`FLX-3513`), since nothing else says which change a row is.
- `before` and `after` are records of the history's columns. `before` is null on a `c`, holds the key and the previous values on a `u`, and holds the last version on a `d`. `after` is null on a `d`.
- `ts_ms` is the event's `date`. `source` carries `version`, `connector` (`flux`), `name` (the Simulation's id), `ts_ms` (the change's instant), `snapshot`, `db` (the product) and `table` (the history's emit). Every other event column becomes a field of `source`, and one named like a field `source` already has is refused (`FLX-3513`).
- Each `d` is followed by a tombstone: the same key, a null value and the same timestamp, so a compacted topic drops the key.

A snapshot is the table's first load in the same envelope, with `op` `r`, `before` null, `after` the row, and `source.snapshot` `true` (`last` on its last record), on its compacted topic. The history's emit, periodic rows and every other expose are written as their rows, one record per row. When the contract binds an expose to a Kafka topic with a registry subject, the subject's schema decides the value, and the stream is written as its rows against that subject whatever its role.

## Schemas

Without a registry, a value is the compact JSON of its object, which needs nothing to read: no spaces, text as it is, and the fields in the order the stream declares them (a row's columns in header order; an envelope as `before`, `after`, `source`, `op`, `ts_ms`). With `registry=URL`, values go through confluent-kafka's registry serializers, and there are two cases.

```text
fluxsim run . --profile ../devices.profile.json --sink kafka:localhost:9092 --sink-opt audience=internal \
  --sink-opt registry=http://localhost:8081
```

- **The contract names a subject** (`binding.properties.registry`). The stream is serialized against that subject, in the format `binding.serialization.value` names (Avro unless it names JSON), at the version `registry.version` names. The sink never registers a schema under a subject the contract names. A subject field that no column fills, a column the subject has no field for, or a value the field's type cannot hold exactly is `FLX-3515`, found before the first record where the schema shows it. A Protobuf subject, a Glue registry and a run with no `registry` for a stream whose contract names a subject are `FLX-3515` too.
- **No subject is named.** The schema is derived from the stream's columns and registered under the synthetic topic's `-value` subject in your registry. It is Avro by default and JSON Schema with `value-format=json`. The Avro types are those of forge's registry derivation: a DATE is `int` with `logicalType: date`, a TIMESTAMP is `long` with `timestamp-millis`, a DECIMAL is `bytes` with `logicalType: decimal`, and every field is nullable.

`registry-auth-env` and `registry-ca-location` are in the credentials section below.

## Exactly once, and resuming

The producer is idempotent and transactional, with a `transactional.id` that is stable for the run, the stream and the topic. A retried delivery therefore fences an earlier one and aborts what it left open. An unpaced stream is one transaction for the whole horizon, committed only when every row has arrived and every record has been acknowledged, and aborted otherwise. A transaction may stay open for `transaction-timeout` seconds (900 by default, the most Kafka's own brokers allow unless they are set higher, and at least 1, the producer's minimum), so a stream that takes longer than 90% of that is aborted and refused with `FLX-3514`, nothing committed. Raise `transaction-timeout` together with the brokers' `transaction.max.timeout.ms`, or pass `commit-every=SECONDS`, which makes an unpaced stream commit every that many seconds as it goes, each commit recording its progress as below. `commit-every` may not be more than a third of `transaction-timeout`.

Each transaction also commits the stream's progress: an offset, under a consumer group named like the `transactional.id`, on partition 0 of the topic, whose value and metadata count the records sent, the order they went in and whether the stream is complete. It commits with the records or not at all. A delivery that starts reads it. A complete stream is not sent again, and a partial one resumes after its last committed record, so running the same command again after a failure sends what is missing and nothing twice.

The brokers drop that progress after `offsets.retention.minutes` (7 days by default), so the run keeps its own record in `sink-receipts.json`: the receipt of each stream delivered and, after every commit the brokers take (each part of a stream sent in parts with `commit-every` or a pace, and the last commit of any stream), a receipt of the stream so far, under `progress`, which the stream's receipt replaces when it lands. A stream that this record says reached that topic of that cluster, whole or in part, is then refused (`FLX-3514`) before anything is sent, since sending it again would duplicate what reached the topic. A topic the delivery has to create holds nothing to duplicate, so a cluster made again under the id it had (a fixed `CLUSTER_ID`, a recreated compose cluster) is delivered to. An entry is matched to the cluster by its id (`clusterId`) when the entry and the brokers both name one, so another spelling of the same brokers (`127.0.0.1:9092` for `localhost:9092`) is still refused; when either names none, it is matched by the target as written. To deliver the stream again on purpose, use another `topic-prefix`, or remove its entry from `sink-receipts.json`. This holds only where the record is: from another machine, or after the run directory was removed, a re-send after the retention duplicates. A part is recorded just after the brokers commit it, so a delivery stopped between the two leaves that last part out of the record. Two deliveries of one run can run at once, to two targets say: each locks the run directory while it rewrites the file, so neither drops the other's entries.

To deliver a run again, to the same topics, send it with the same key and value settings. The progress also records the settings that decide a record's bytes (the key columns and format, the event-time column and its default, and the value format, subject and pinned version), and a stream delivered under other ones is refused with `FLX-3504`, since its topic would hold two shapes. Deliver it to another topic with `topic-prefix` or `topic.EXPOSE`, or delete the topic and the consumer group the message names.

Read with `isolation.level=read_committed` to see each record once. Java's consumer reads `read_uncommitted` unless told otherwise, and it would see the records of an aborted attempt.

## Pace a stream

`pace=max`, the default, sends as fast as the brokers take, in the stream's order. `pace=realtime` and `pace=xN` (N a number of at least 0.001; `x2` is twice as fast as real time) hold the stream until its last row, put its records in event-time order while keeping each entity's order, and send each one when the wall clock, counted from the first record, reaches its offset from the first event time divided by N.

```text
fluxsim run . --profile ../devices.profile.json --sink kafka:localhost:9092 --sink-opt audience=internal \
  --sink-opt pace=x3600 --sink-opt commit-every=2
```

A paced stream commits a transaction at least every `commit-every` seconds (1 by default) and before any wait longer than that, so a read-committed consumer sees records as they are sent, and each transaction carries the progress above. A delivery resumes only in the order it began: a paced delivery and an unpaced one of the same stream do not mix (`FLX-3504`). Keys, values, timestamps and headers are the same at every pace and every `--jobs`. Pacing chooses when a record is sent and never what it holds. A paced stream is held in memory until its last row arrives (one whose keys and values pass 1 GiB is refused with `FLX-3504`, before anything is sent: use `pace=max`), and streams are delivered one after another, so pacing orders the records within a stream and not across streams.

## Performance

The figures below were measured with one delivery process on a 16-core laptop that was running other work, against a local single-node Kafka 4.4 and a Karapace registry in Docker, at `pace=max`, on a bundle whose change events number about 200,000 (about 209,000 records, since each delete adds a tombstone). One Python process builds, serializes and queues every record, so the figures are that process's.

| Stream | Records per second | Values, megabytes per second | Every stream of the bundle (2.09 million records) |
|---|---|---|---|
| Change events, plain JSON | about 41,000 | about 11 | about 28,000 records per second, 75 s |
| Change events, Avro through the registry | about 23,000 | about 2 | about 28,000 records per second, 75 s |

Planning, generating and applying that bundle took about 30 s, so a run into Kafka took about 105 s from the plan to the last commit, for either value format. Each figure for the events stream is the best of three deliveries on fresh topics. They move by a quarter or more with what else the machine is doing, so read them as an order of magnitude and measure your own from a checkout of the repository with `python -m tests.bench.kafka_real_broker`, which starts its own broker and registry in Docker and removes them.

For each record the process finds its key and event time, builds its value (the envelope for a change event) and writes its bytes. `make bench` times that work alone, over a broker that keeps nothing: on the same machine, change events took about 105,000 records per second of processor time as plain JSON and about 39,000 as Avro. Plain JSON is the cheapest. Avro and JSON Schema first convert each value to its field's exact type (a decimal text to a decimal that fits its precision and scale, a date to its day number), and Avro then encodes it. Registering the schema and building the converters happen once, when a stream opens, and no record asks the registry for anything. A paced stream is limited by its clock, not by these figures.

Two options matter. `pace` is the one that changes the figures most: `max` sends as fast as the process can build records, and `realtime` or `xN` waits for each record's time. `compression` sets the codec of the producer's batches (`none`, `gzip`, `snappy`, `lz4` or `zstd`; `lz4` by default). On a broker on the same machine the codecs were within the noise of one another; the codec is chosen for the network and the brokers' disks, where fewer bytes are worth the cheap compression. An unpaced stream also batches for 20 ms before it sends, so the brokers get few, large requests; a paced stream keeps librdkafka's 5 ms so no record waits long past its time.

## Credentials

A credential is never an option (`FLX-3508`) and never part of the target. SASL credentials come from the environment variables that `sasl-username-env` and `sasl-password-env` name, with `security-protocol` and `sasl-mechanism` (`PLAIN`, `SCRAM-SHA-256` or `SCRAM-SHA-512`). The registry's basic credentials come from the variable `registry-auth-env` names, as `USER:PASSWORD`.

```text
export KAFKA_USER=...
export KAFKA_PASSWORD=...
fluxsim run . --profile ../devices.profile.json --sink kafka:broker-1.example.org:9093 --sink-opt audience=external \
  --sink-opt security-protocol=SASL_SSL --sink-opt sasl-mechanism=SCRAM-SHA-512 \
  --sink-opt sasl-username-env=KAFKA_USER --sink-opt sasl-password-env=KAFKA_PASSWORD \
  --sink-opt ssl-ca-location=/etc/ssl/kafka-ca.pem
```

A credential is sent only over TLS (`SASL_SSL`, an `https` registry), except a SASL credential to brokers that are all on this machine's loopback addresses. The registry's credential never goes in clear: with `registry-auth-env` every registry URL must be `https` (`FLX-3504`), because the registry client follows the environment's proxies (`HTTP_PROXY`, `ALL_PROXY`) and would send the header to a proxy in clear even for a registry on this machine. An `http` registry is for one without credentials. Over `SASL_PLAINTEXT`, which is allowed only there, use `sasl-mechanism=SCRAM-SHA-256` or `SCRAM-SHA-512`: `PLAIN` sends the password itself and is refused (`FLX-3504`). With `SASL_PLAINTEXT` the sink also reads the brokers the cluster advertises, on every client it opens (the admin client, the producer and the progress consumer), and refuses (`FLX-3504`) one that is not on this machine. A name counts as this machine only when it resolves and every address it resolves to is loopback: `localhost` is not trusted by its spelling, and `0.0.0.0` or a name that does not resolve is not loopback. `registry` may list several URLs separated by commas, each checked the same way (with `registry-auth-env`, all `https`). `ssl-ca-location` and `registry-ca-location` name certificate files. A missing variable is `FLX-3504`, naming the variable and never a value, and an error a library raises is scrubbed of every credential before it is shown. librdkafka's log goes to the `flux_engine.sinks.kafka` logger, never straight to the terminal, and each line is scrubbed of the credentials like an error.

## The options

| Option | Meaning |
|---|---|
| `audience` | `internal` or `external`; required |
| `registry`, `registry-auth-env`, `registry-ca-location` | the registry, and where its credentials and certificates come from |
| `topic-prefix`, `topic.EXPOSE` | the topic's prefix and a stream's name |
| `key.EXPOSE`, `key-format` | the key columns, and `string` or `json` |
| `timestamp.EXPOSE` | the timestamp column |
| `value-format` | `json` for a derived JSON Schema instead of Avro |
| `pace`, `commit-every` | `max`, `realtime` or `xN` (N at least 0.001); and the seconds between transactions (at least 0.1; 1 by default for a paced stream; an unpaced stream is one transaction unless `commit-every` is given) |
| `transaction-timeout` | the seconds a transaction may stay open, 900 by default; at least 1 (the producer's 1000 ms), at most the brokers' `transaction.max.timeout.ms`, and at most 2147483 |
| `compression` | `none`, `gzip`, `snappy`, `lz4` (the default) or `zstd`: the codec of the producer's batches |
| `partitions`, `replication-factor` | for a topic the sink creates; at most 2147483647 and 32767 |
| `timeout` | the time limit for a broker or registry call, in seconds; at most 3600 |
| `security-protocol`, `sasl-mechanism`, `sasl-username-env`, `sasl-password-env`, `ssl-ca-location` | the connection |

Any other option is `FLX-3504`, as for every sink.

## Read the receipt

Every record carries headers that label it synthetic and name its stream: `flux.synthetic` (`true`), `flux.run-id`, `flux.expose`, `flux.output-digest` and `flux.row`, the row's position in the stream.

The run's `sink-receipts.json` holds one receipt for each stream. It names the topic and its partition count, the shape (`debezium` or `row`), the value format and subject, the key format, the pace, the compression, the transactions, the records sent and the records skipped as already delivered, the audience, and the grade. The grade is the run's, or, when `--profile` was given, the weaker of the run's and the one worked out from the profile, which is also recorded as `profileGrade`. `gradeSource` says which the grade was held to: `profile`, or `plan` when the grade is the run's own record alone. A stream that stopped after committing part of itself has an entry under `progress` instead, a receipt of the stream so far that counts the records `committed`. The file holds no credential.

The streams are also in the run directory as NDJSON, as for every sink, and those files are what the `outputDigest` covers. The shapes the sink reads are in [ADR 0027](../adr/0027-a-history-projects-snapshots-events-and-periods.md), and the grades it gates on are in [ADR 0020](../adr/0020-privacy-grades.md).
