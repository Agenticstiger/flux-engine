# ADR 0038: a Kafka sink keys every record by its entity, delivers each stream exactly once in event time, writes a history's changes in Debezium's envelope, paces outside the digest, and sends only what its audience may see

## Status

Accepted 2026-11-24. Builds on [ADR 0017](./0017-an-entity-is-a-chain-of-versions.md) (an entity is a chain of
versions), [ADR 0020](./0020-privacy-grades.md) (privacy grades), [ADR 0027](./0027-a-history-projects-snapshots-events-and-periods.md)
(a history projects a snapshot, change events and periodic rows) and
[ADR 0031](./0031-the-report-the-story-and-publishing.md) (sinks take credentials only from the environment).

## Context

A simulation lands in files, databases, Iceberg and Delta through the sinks of `flux_engine.sinks`. Kafka is where
many consumers read data: keyed records in the partitions a Java producer would choose, event time in the record
timestamp, values in a schema registry's schemas, a table's changes as change-data-capture records, and no duplicates
when a delivery is retried. A sink that writes that shape lets those consumers read synthetic data with the code
they already run. Two rules hold for every sink and shape this one. The rows a sink receives are the digest's own
pre-image, so nothing a sink does (how fast, in what order, through which client) may change them. And a run's privacy
grade (ADR 0020) says what its numbers rest on, so a stream for readers outside the data's owner must carry the grade
those readers need.

## Decision

### 1. The sink and its target

`fluxsim run BUNDLE --sink kafka:HOST:PORT[,HOST:PORT...]` delivers every stream of the run to the Kafka cluster
those bootstrap servers name. The sink is the optional `[kafka]` extra, on confluent-kafka-python 2.16 (Apache-2.0,
over librdkafka), a built-in sink of `flux_engine.sinks.registry` that needs the extra the way the Delta sink needs
`[publish]`: its factory is imported only when it is selected, so listing sinks and every run without it load none of
confluent-kafka, and selecting it without the extra is `FLX-3501`, naming `pip install 'flux-sim-engine[kafka]'`.
Its settings are `--sink-opt
KEY=VALUE`, a closed set (section 12); an unknown one is `FLX-3504`, as for every sink. Nothing is contacted until
every check that needs no network has passed: the options when the target is opened, before the run, and the gate
(section 2) and each stream's declarations (sections 4 to 7) when the sink is built for a stream, before any row.

### 2. The audience gate

Every Kafka target declares its audience, `--sink-opt audience=internal` or `audience=external`; there is no
default, and a target without one is `FLX-3504`. Internal readers work for the data's owner; external readers do
not. The minimum grade is `P1` (checked) for an internal audience and `P3` (differentially private marginals) for
an external one, compared in ADR 0020's order, so `P3`, `P4` and `P0` reach an external audience and `P5` reaches
neither.

The grade is worked out again from the run's bound record and its bundle, never taken from `plan.json`'s
`privacyGrade` alone. Before delivery the engine checks the record as `apply` does: the plan still digests to its
`planDigest`, it is this run directory's plan, and `run-digest.json` names it. It then checks that `receipt.json`
belongs to that plan, by three comparisons (the receipt's `planDigest` is the plan's, its `runDigest` is the digest of
`run-digest.json`, and each stream's `outputDigest` is the component `run-digest.json` records for that expose), and
that the bundle is the plan's (its input digest, worked out as `apply` works it out, is the plan's `inputDigest`).
The grade is then read the way `apply` reads it: what the plan records, clamped for a run that pins a profile (a
recorded `P0` or `P4` is read as `P1`), and weakened by every generator plugin the bundle declares (ADR 0032). The
pin and the plugins are read from the bundle, which `inputDigest` covers. The result goes to the sink with the
stream (`StreamContext.privacy_grade`).

An internal audience is held to that grade. An external audience is held to a grade worked out from the pinned
profile itself, as `ship` works it out, so `--profile` must be given to `run` and to `apply --run-id`, and must be the
profile the Simulation pins (`FLX-3109`, `FLX-3110`). The receipt's writer records it as `profileGrade`, and its
`privacyGrade` is then the weaker of the run's grade and that one, so a receipt never records a better grade than
the profile gives, for either audience. Its `gradeSource` says where the grade comes from: `profile` then, and
`plan` when no profile graded the run, so a grade read from the run's own files alone is not taken for one checked
against the profile. A plan alone never unlocks an external target.

The gate fails closed: a run that pins no profile and declares no plugin has no grade and goes to no Kafka target; a
plan, run digest or receipt that does not check, a bundle that is not the plan's, or a stream handed no grade at all
is refused with `FLX-3512` for every audience, naming what is missing; so is an external target given no profile or
a profile that is not the pinned one (the message names `--profile`), and so is a grade below the audience's
minimum. All of it happens before anything is contacted, and the refusal names the grade, the audience and its
minimum, never a value of the data. Other sinks are not gated: they receive the same rows as before. A column's
`sensitivity` floor stays where ADR 0020 put it, at `plan`, `apply` and `ship`.

#### Threat model

The state directory is writable by whoever runs the engine, so the files of a run are evidence, not authority.

- An internal audience trusts the run's bound record: the plan digest, `run-digest.json`, and the receipt and its
  streams bound to them. Editing the plan's grade, or swapping in another run's receipt and streams, is refused.
- An external audience is re-derived from the pinned profile, which the command must be given. A forged
  `privacyGrade` in the plan does not reach it.
- What stays possible: someone able to rewrite every file of a run directory consistently (plan, run digest, receipt
  and streams) can make a run look like another one. For an external target that still needs the pinned profile to
  grade `P3`.
- A run that pins no profile cannot go to an external audience.

### 3. What the sink reads, and what it touches

The engine also hands each stream's sink the bundle directory and the Simulation's id
(`StreamContext.bundle_directory`, `StreamContext.simulation_id`), only so that the sink can find the stream's
declarations: the contract's keys and Kafka binding, and the Simulation's `history`, `snapshots`, `events` and
`periods` entries. The sink never reads a grade from them; the engine does (section 2). The contract digest of the
header the sink is opened with must be the bundle's, else `FLX-3504`.

The sink connects to the bootstrap servers of its target, the brokers they name, and the registry its `registry`
option names, and to nothing else. A registry URL, a topic or a credential written in the contract is never
contacted: the contract names a subject, and the operator names the registry that holds it. The synthetic data
never goes to the topic the contract names either (section 4).

### 4. Topics

A stream's topic is a prefix and a name. The name is `--sink-opt topic.EXPOSE=NAME`, else the contract's
`binding.location.topic` when the expose is bound to Kafka, else the expose id. The prefix is `topic-prefix`, `sim.`
by default, and it may not be empty, so a synthetic topic never has the name of the topic the contract declares.
The whole name must be a legal Kafka topic name, and two streams of one delivery may not share a topic
(`FLX-3513`). A synthetic topic may not be the topic any expose's contract binding names (`FLX-3513`), and the
`<topic>-value` subject a derived schema is registered under may not be a subject any binding names (`FLX-3515`):
the refusal names both exposes and the option (`topic-prefix` or `topic.EXPOSE`) that gives one of them another
name.

A missing topic is created with the broker's default partitions and replication unless `partitions` and
`replication-factor` say otherwise; a snapshot's topic is created with `cleanup.policy=compact`. An existing topic
is checked first (`FLX-3514`): its `message.timestamp.type` must be `CreateTime`, since `LogAppendTime` would
replace event time with the time of the write, and a snapshot's topic must be compacted.

### 5. Keys and partitions

Every record is keyed by its entity, so each entity's records keep their order in one partition. The entity of a
history's emit and of each projection of it (its snapshot, events and periodic rows) is the history's natural
entity (ADR 0017); the entity of any other expose is its primary entity. `key.EXPOSE=COLUMN[,COLUMN...]` names the
key columns instead. An expose with neither is `FLX-3513`, naming the option. `key-format=string`, the default,
writes a one-column key as its rendered text, as Kafka's `StringSerializer` would; `key-format=json` writes the key
columns as one compact JSON object, the columns in the order `key.EXPOSE` names them, the shape Debezium's JSON
converter gives a key. A key of several columns needs `json`, and a null key value is `FLX-3513`, naming the column
and the row's position.

The producer's partitioner is librdkafka's `murmur2_random`: Java's murmur2 over the key bytes, so a record lands
on the partition a Java producer would choose for the same key bytes and partition count. librdkafka's own default,
`consistent_random`, chooses differently.

### 6. Event time

The record timestamp is the event's time, set explicitly, in milliseconds since 1970 in UTC (a DATE at its
midnight):

| stream | timestamp |
|---|---|
| a history's emit | the version's validity start |
| events | the `date` column (when the change was recorded) |
| snapshot | the validity start of the entity's last version when the snapshot carries it, else the history's `until` |
| periodic rows | the period column |
| any other expose | `timestamp.EXPOSE=COLUMN`, else the contract's `defaultAggTimeDimension`, else its one time dimension |

An expose with no timestamp column is `FLX-3513`, naming the option. Before each record is sent, its timestamp is
checked against the topic's `message.timestamp.before.max.ms` and `message.timestamp.after.max.ms` (KIP-937; the
latter is one hour by default from Kafka 4.0) and against the epoch: a timestamp at or before
1970-01-01T00:00:00Z is refused, since brokers refuse a negative timestamp and a producer reads 0 as "the current
time". A record outside them stops the stream with `FLX-3514`, naming the limit and the remedy: a topic configured
for a backfill, or a run window nearer the present (after the epoch). The record's bytes never move to fit a limit.

### 7. Values: rows, Debezium's envelope and tombstones

A history's events (ADR 0027 section 4) are written in Debezium's envelope:

- `op` is the event's `op` column, `c`, `u` or `d`. An events entry without `op` is `FLX-3513`: nothing else says
  which change a row is.
- `before` and `after` are records of the history's columns. A field comes from the event column the entry's
  `before` or `after` maps to it, or from the event column named like it, which holds the version the event is
  about. `before` is null on a `c`, and holds the key and the mapped previous values on a `u` and the last version
  on a `d`; `after` is null on a `d`.
- `ts_ms` is the event's `date`, and `source` is `{version, connector: "flux", name, ts_ms, snapshot, db, table}`:
  the engine's version, the Simulation's id, the change's instant (`at`, else `date`), the product and the
  history's emit. Every other event column is a field of `source`, as a connector's own fields are, and one named
  like a field `source` already has is `FLX-3513`.
- Each `d` is followed by a tombstone: the same key, a null value and the same timestamp, so a compacted topic drops
  the key.

A snapshot is the table's first load, in the same envelope with `op: r`, `before` null, `after` the row and
`source.snapshot` `true` (`last` on its last record), on its compacted topic. A history's emit, periodic rows and
every other expose are written as their rows: one record per row, the value an object of the row's columns.

When the contract binds an expose to a Kafka topic with a registry subject (`binding.properties.registry`), the
subject's schema decides the value, so the stream is written as its rows against that subject whatever its role.

### 8. Schemas

With `--sink-opt registry=URL`, values go through confluent-kafka's registry serializers:

- An expose whose contract names a subject is serialized against it, in the format `binding.serialization.value`
  names (Avro unless it names JSON), at the version `registry.version` names. The sink never registers a schema
  under a subject the contract names. A field of the subject that no column fills, a column the subject has no
  field for, or a value the field's type cannot hold exactly is `FLX-3515`, found before the first record is sent
  where the schema shows it. A Protobuf subject and a Glue registry are `FLX-3515` in this version.
- Any other stream's schema is derived from its columns (Avro by default, `value-format=json` for JSON Schema) and
  registered under the synthetic topic's `-value` subject in the named registry. The Avro types are those of
  forge's registry derivation: a DATE is `int` with `logicalType: date`, a TIMESTAMP is `long` with
  `timestamp-millis`, a DECIMAL is `bytes` with `logicalType: decimal`; every field is nullable.

Values are converted exactly. A decimal is checked against `decimal(precision, scale)` in a context wide enough for
any value that fits (`decimal(38, s)` included), so only a value that does not fit is refused. An Avro `int` holds
-2^31 to 2^31-1 and a `long` -2^63 to 2^63-1; a value outside the range is `FLX-3515`, naming the field and never
the value.

How the bytes are written does not change what they are. A derived Avro schema is registered once, when the
stream opens, and each value is then written with fastavro behind the registry's framing (a zero byte and the
schema's id), the bytes confluent-kafka's `AvroSerializer` writes for the same schema and value. A subject the
contract names is written the same way when its schema carries no `ruleSet` and references no other schema; a
schema with data-contract rules keeps confluent-kafka's serializer, since the rules run inside it. A derived JSON
Schema is registered once and each value is written as compact JSON behind the same framing, the bytes
`JSONSerializer` writes. It does not validate each value again with `jsonschema`: the value converter, built from
the same schema, already holds every field to the schema's type and nullability and writes no field the schema
does not list. A contract's JSON subject keeps `JSONSerializer` and its validation, since the schema is the
author's.

Without `registry`, a value is the compact JSON of its object with the rendered values (no spaces, text written as
it is, not escaped), which needs no registry to read. The fields come in the order the stream declares them: a
row's columns in the order of its header, an envelope as `before`, `after`, `source`, `op`, `ts_ms`, and a
record inside it in the order of the history's columns. The order is a function of the declarations, so the
bytes are the same at every pace and every `--jobs`, and no sorting of every object's keys (RFC 8785) is paid for
on every record; an expose whose contract names a subject is then `FLX-3515`, since its readers expect
registry framing.

### 9. Exactly once: one transaction per horizon, and progress inside it

The producer is idempotent and transactional: `enable.idempotence` (librdkafka leaves it off by default) and a
`transactional.id` stable for the run, the stream and the topic, so a retried delivery fences an earlier one and
aborts what it left open. The producer is opened before the stream's progress is read, so a producer of an earlier
delivery is fenced first and nothing it still holds can commit after the read; a stream found complete closes the
producer again and sends nothing. A stream unpaced (`pace=max`) is one transaction, the whole horizon, committed only
when every row arrived and every record was acknowledged, and aborted otherwise.

A transaction is bounded by `transaction-timeout=SECONDS` (900 by default, Kafka's default for the brokers'
`transaction.max.timeout.ms`; at least 1, the producer's minimum of 1000 ms), which is the producer's
`transaction.timeout.ms` and no longer depends on `timeout`.
A brokers' setting below it fails the producer's `init_transactions` with `FLX-3505`, naming both
`transaction-timeout` and `transaction.max.timeout.ms`. An unpaced stream that is not given `commit-every` must fit in
its one transaction: when the open transaction reaches 90% of `transaction-timeout` (checked every 1,024 records and
before the commit flushes) it is aborted and the stream refused with `FLX-3514`, nothing committed, naming
`transaction-timeout`, the brokers' setting and `commit-every=SECONDS`. With `commit-every`, an unpaced stream also
commits every that many seconds (the age is checked every 1,024 records), each commit recording its progress as a
paced stream's does, so resume and exactly once hold. A `commit-every` above a third of `transaction-timeout` is
`FLX-3504`.

Each transaction also commits the stream's progress, the pattern Kafka Connect's exactly-once source support uses
(KIP-618): an offset under a consumer group named like the `transactional.id`, on partition 0 of the topic, whose
value and metadata count the records sent, the order they were sent in, and whether the stream is complete. The
offset commits with the records or not at all. A delivery that starts reads it: a complete stream is not sent again,
and a partial one resumes after its last committed record. A read-committed consumer (`isolation.level` is
`read_committed` by default in librdkafka and `read_uncommitted` in Java) sees each record exactly once, across a
failure, a forced abort and a second delivery of the same run.

The brokers drop that progress: it is a memberless group's committed offset, and the brokers expire it after
`offsets.retention.minutes` (7 days by default). Without it a re-run would send every finished stream again, resend
the committed part of an unfinished one, and lose the fingerprint guard below, so the run keeps a record of its own in
`sink-receipts.json`. It holds the receipt of each stream delivered and, after every commit the brokers take (each
part of a stream sent in parts with `commit-every` or a pace, and the last commit of any stream), a receipt of the
stream so far under `progress`, counting the records committed; the sink hands that receipt to the engine
(`StreamContext.record_committed`) once the brokers have committed, and the stream's own receipt replaces every such
entry of that stream on that cluster when it lands. The engine hands each stream what the record
holds for it (`StreamContext.delivered` and `StreamContext.committed`; only the sink that needs them reads them). When
the stream's topic is already there, the brokers hold no progress for the stream and an entry of the same sink and
topic counts records sent, skipped or committed there, on the same cluster, the stream is refused with `FLX-3514`
before anything is sent. A topic the delivery has to create holds nothing to duplicate, so a cluster made again under
the id it had (a fixed `CLUSTER_ID`, a recreated compose cluster) is delivered to. An entry is about
the same cluster when it names the cluster id the brokers name (`clusterId`), however the target was written; when the
entry or the brokers name none, when it names the same target. The message says the brokers no longer hold the
progress, that sending again would duplicate what reached the topic, and the two ways to do that on purpose: another
`topic-prefix`, or removing that stream's entry from `sink-receipts.json`. The bounds that remain: without the local
record (another machine, or a removed run directory) a re-send after the retention duplicates, and a part is recorded
just after the brokers commit it, so a delivery stopped between the two leaves that last part out of the record.
Every write of the record (a receipt, a part) holds an advisory lock across its read, change and write: `fcntl.flock`
on the run directory, or `msvcrt.locking` on a lock file beside it where there is no `fcntl`. Two deliveries of one
run, to two targets say, therefore keep each other's entries.

The progress also records a fingerprint (16 hex digits of a digest) of the settings that decide a record's bytes
beside its row: the key columns and the key format, the event-time column and its default, and the value form
(plain JSON, or the registry's value format, subject and a contract subject's pinned version, never the schema id).
A stream found complete or partial under another fingerprint is refused with
`FLX-3504`, since resuming or skipping it would mix two shapes in one topic: deliver it to another topic
(`topic-prefix` or `topic.EXPOSE`), or delete the topic and the consumer group.

The producer's batching suits the stream. An unpaced stream waits up to 20 ms to fill a batch of up to 100,000
records and queues up to a million, so the brokers are sent few, large requests; a paced stream keeps librdkafka's
5 ms, so a record is not held back from its time. `compression=none|gzip|snappy|lz4|zstd` sets the codec of the
batches (`lz4` by default) and is recorded in the receipt. The producer drains its delivery reports once for every
1,000 records, and waits 50 ms and tries again when its queue is full.

### 10. Pacing outside the digest

`pace=max` (the default) sends as fast as the brokers take, in the stream's order. `pace=realtime` and `pace=xN` (N a
number of at least 0.001, `x2` twice as fast as real time) hold the stream until its last row, put its records in
event-time order while keeping each entity's order, and send each one when the wall clock, counted from the first
record, reaches its offset from the first event time divided by N. A paced stream commits a transaction at least every
`commit-every` seconds (1 by default; an unpaced stream commits only when `commit-every` is given) and before every
wait longer than that, so read-committed consumers see the records as they are sent, and each transaction carries the
progress of section 9. A delivery resumes only in the order it began: a paced delivery and an unpaced one of the same
stream do not mix (`FLX-3504`). Keys, values, timestamps and headers are the same at every pace and every `--jobs`:
pacing chooses when a record is sent, never what it holds. A paced stream is held in memory until its last row, so one
whose serialized keys and values pass 1 GiB is refused with `FLX-3504` before anything is sent, pointing to
`pace=max`.

### 11. Headers and the receipt

Every record carries headers that label it synthetic and name its stream: `flux.synthetic: true`, `flux.run-id`,
`flux.expose`, `flux.output-digest` and `flux.row`, the row's position in the stream. The receipt names the topic,
its partition count, the shape (`debezium` or `row`), the value format and subject, the key format, the pace, the
compression, the transactions, the records sent and skipped as already delivered, the audience, the grade and, when
the command was given the pinned profile, the grade worked out from it (`profileGrade`); the grade is then the weaker
of the run's and that one, and `gradeSource` says which it was held to (`profile` or `plan`). A stream that stopped
after committing part of itself has, under `progress` instead, a receipt of the stream so far that counts the records
`committed`. Neither holds a credential.

### 12. Credentials and options

A credential is never an option (`FLX-3508`) and never part of the target. SASL credentials come from the environment
variables `sasl-username-env` and `sasl-password-env` name, with `security-protocol` and `sasl-mechanism` (`PLAIN`,
`SCRAM-SHA-256` or `SCRAM-SHA-512`); the registry's basic credentials come from the variable `registry-auth-env`
names, as `USER:PASSWORD`. A credential is sent only over TLS (`SASL_SSL`, an `https` registry), except a SASL
credential to brokers that are all this machine's loopback addresses. The registry's credential never goes in clear:
with `registry-auth-env`, every registry URL must be `https` (`FLX-3504`), because the registry client follows the
environment's proxies (`HTTP_PROXY`, `ALL_PROXY`) and would send the `Authorization` header to a proxy in clear even
for a registry on this machine. An `http` registry, on this machine or another, is allowed only without credentials.
`ssl-ca-location` and `registry-ca-location` name certificate files. A credential over `SASL_PLAINTEXT`, allowed only
on this machine, needs `sasl-mechanism=SCRAM-SHA-256` or `SCRAM-SHA-512`: `PLAIN` sends the password itself and is
`FLX-3504`, as is a `transaction-timeout` below 1 second or above 2147483 seconds (librdkafka takes 1000 ms to an
int32 of milliseconds). When a
credential goes over `SASL_PLAINTEXT` on this machine, the first request of every client reads the brokers the cluster
advertises (the admin client's metadata, the producer's before `init_transactions` and the progress consumer's before
its read), and one that is not a loopback address is `FLX-3504` before anything else is asked. A name is this machine
only when it resolves and every address it resolves to is loopback (an IPv4-mapped IPv6 address counts by its IPv4
address; `0.0.0.0` and a name that does not resolve are not loopback; `localhost` is not trusted by its spelling): the
bootstrap servers may be local while the brokers they name are not, and the credential would go to them in the clear.
`registry` may name several URLs, separated by commas, as the registry client fails over between them; each is checked
by the same rules, and with `registry-auth-env` every one must be `https`. The registry client carries the `timeout`
option, is opened when a stream needs it and closed when that stream's delivery ends, and every client is told not to
send the brokers metrics of its own (`enable.metrics.push` off, KIP-714). A missing variable is `FLX-3504`, naming the
variable and never a value, and an error a library raises is scrubbed of every credential before it is shown.
librdkafka's log goes to the `flux_engine.sinks.kafka` logger, never to the terminal directly.

The options are `audience`, `registry`, `registry-auth-env`, `registry-ca-location`, `topic-prefix`,
`topic.EXPOSE`, `key.EXPOSE`, `key-format`, `timestamp.EXPOSE`, `value-format`, `pace`, `commit-every`,
`transaction-timeout`, `compression`, `partitions`, `replication-factor`, `timeout`, `security-protocol`,
`sasl-mechanism`, `sasl-username-env`, `sasl-password-env` and `ssl-ca-location`. Their numbers are bounded, each
refusal `FLX-3504` naming the option and the limit: `partitions` at most 2147483647, `replication-factor` at most
32767, `pace=xN` with N at least 0.001, `commit-every` at least 0.1 seconds, `timeout` at most 3600 seconds and
`transaction-timeout` from 1 to 2147483 seconds. librdkafka's log lines go through the same scrubber as an error.

Metadata that is not yet settled is waited out, up to `timeout`: a retriable error on a topic or a partition (a leader
not yet elected) and a topic the configuration read does not yet know right after it was created. A flush that still
holds unacknowledged records is repeated while their number falls, and is a failure only after a whole `timeout`
with no progress. A progress read that a partition error answers is `FLX-3505`, never "no progress".

## Consequences

- The engine's bytes and digests do not change: the sink reads the rows the digest covers and adds nothing to them.
- A run's privacy grade, worked out from its bound record and bundle, travels with its streams, so a stream for
  outside readers is refused before it leaves, and a run that pins no profile and declares no plugin is not streamed
  to Kafka at all. An external target needs `--profile`.
- Kafka consumers read synthetic records with the keys, partitions, timestamps, schemas and change-data-capture
  shape of the real topics, and can tell them apart by their headers and their topic's prefix.
- A paced stream is held in memory until its last row arrives.
- Streams are delivered one after another, so pacing orders records within a stream, not across streams.

## What would change this decision

- A Protobuf subject, a Glue registry or OAuth credentials asked for by a real deployment.
- A need to interleave several topics in one event-time order, which needs the deliver stage to hand the sink every
  stream at once.
- A consumer that needs Debezium's connector-specific `source` fields, which the sink would fill from the bundle.

## Prior art

- confluent-kafka-python 2.16.2 and librdkafka 2.16.0 (Apache-2.0, BSD-2-Clause): idempotence off by default,
  `consistent_random` by default, `murmur2_random` for Java's partitioning, explicit timestamps, transactions and
  `send_offsets_to_transaction`, and the registry serializers this sink uses.
- fastavro 1.13 (MIT): the Avro writer confluent-kafka's `AvroSerializer` calls for each record. The sink calls it
  directly behind the registry's framing once the schema's id is known, and the tests compare the bytes.
- KIP-618 (Kafka Connect exactly-once source support): records and their source progress in one transaction, and
  fencing by a stable `transactional.id`.
- KIP-937 and KIP-1030: the `message.timestamp.before.max.ms` and `after.max.ms` limits, and their defaults.
- Debezium: the change event envelope (`before`, `after`, `source`, `op`, `ts_ms`), `op` values `c`, `u`, `d` and
  `r`, `source.snapshot` values `true`, `last` and `false`, and a tombstone after a delete.
- Karapace 6.2.4 (Apache-2.0): a registry with Confluent's REST API, which the integration tests run.
- ShadowTraffic's per-key state machines produce CDC-shaped streams too; here the changes come from declared
  histories instead.
