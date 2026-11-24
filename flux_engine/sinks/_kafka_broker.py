# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The Kafka sink's only contact with a broker: the protocols it needs and the confluent-kafka adapter.

:class:`Broker` and :class:`TxnProducer` say what the sink asks of a cluster: describe or create a
topic, read a stream's committed progress, and send records in a transaction that also commits that
progress. :class:`ConfluentBroker` answers them with confluent-kafka's admin client, a consumer
that never subscribes (it only reads and commits offsets) and an idempotent transactional producer.
Every call to the library is here; the library is imported inside the methods that use it, so this
module imports without it and a test replaces the whole adapter.

Every error a library raises is shown scrubbed (:func:`~flux_engine.sinks._secrets.scrub`: the
target, the option values and the credential values the operator gave) as ``FLX-3505``, and the
library's exception is never chained. librdkafka's log goes to the ``flux_engine.sinks.kafka``
logger, and its error callback is kept for the message of a failure.

Standard library and :mod:`flux_engine` only, until a method runs.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from flux_engine.core.errors import FluxError
from flux_engine.sinks._secrets import Known, scrub
from flux_engine.sinks.errors import FLX_3505

__all__ = [
    "TOPIC_CONFIGS",
    "Broker",
    "ClientSettings",
    "ConfluentBroker",
    "Credentials",
    "TopicInfo",
    "TxnProducer",
]

LOGGER = logging.getLogger("flux_engine.sinks.kafka")

#: The topic configuration the sink reads: the effective values it checks a stream against.
TOPIC_CONFIGS: Tuple[str, ...] = (
    "cleanup.policy",
    "message.timestamp.type",
    "message.timestamp.before.max.ms",
    "message.timestamp.after.max.ms",
)

#: The librdkafka partitioner that picks the partition Java's murmur2 picks.
PARTITIONER = "murmur2_random"


@dataclass(frozen=True)
class TopicInfo:
    """A topic as the brokers hold it: its partitions and the effective values of :data:`TOPIC_CONFIGS`."""

    partitions: int
    configs: Mapping[str, str]


class TxnProducer(Protocol):
    """An idempotent producer inside one ``transactional.id``."""

    def begin(self) -> None:
        """Begin a transaction."""

    def produce(
        self,
        topic: str,
        key: bytes,
        value: Optional[bytes],
        timestamp_ms: int,
        headers: Sequence[Tuple[str, bytes]],
    ) -> None:
        """Queue one record; raises when a delivery already failed."""

    def commit(self, *, topic: str, records: int, metadata: str) -> None:
        """Flush, add the stream's progress to the transaction and commit it.

        Raises when any record was not acknowledged.
        """

    def abort(self) -> None:
        """Abort the open transaction."""

    def close(self) -> None:
        """Release the producer."""


class Broker(Protocol):
    """What the Kafka sink asks of a cluster."""

    def describe(self, topic: str) -> Optional[TopicInfo]:
        """The topic, or ``None`` when it does not exist."""

    def create(
        self,
        topic: str,
        *,
        partitions: Optional[int],
        replication_factor: Optional[int],
        configs: Mapping[str, str],
    ) -> TopicInfo:
        """Create the topic (the brokers' defaults for what is ``None``) and describe it."""

    def progress(self, group: str, topic: str) -> Optional[str]:
        """The metadata *group* committed on partition 0 of *topic*, or ``None``."""

    def transactional_producer(self, transactional_id: str, group: str) -> TxnProducer:
        """A producer under *transactional_id* whose transactions commit offsets of *group*.

        Initialising it fences any producer under the same id and aborts what that one left open.
        """

    def versions(self) -> Mapping[str, str]:
        """The client library's versions, for the receipt; empty when there are none."""

    def close(self) -> None:
        """Release the clients this broker holds; a later call opens them again."""


@dataclass(frozen=True)
class Credentials:
    """What the environment holds for the options that name variables; never printed."""

    sasl_username: Optional[str] = field(default=None, repr=False)
    sasl_password: Optional[str] = field(default=None, repr=False)
    registry_auth: Optional[str] = field(default=None, repr=False)


@dataclass(frozen=True)
class ClientSettings:
    """The connection settings of the brokers: no credential, only where and how."""

    bootstrap: str
    timeout: float = 30.0
    security_protocol: Optional[str] = None
    sasl_mechanism: Optional[str] = None
    ssl_ca_location: Optional[str] = None


def _first_line(text: str) -> str:
    lines = text.strip().splitlines()
    return (lines or [""])[0][:300]


class ConfluentBroker:
    """:class:`Broker` over confluent-kafka's ``AdminClient``, ``Consumer`` and ``Producer``.

    *credentials* is called when a client is first needed (never when the target is built) and
    *known* when an error is shown: the strings a message is scrubbed of.
    """

    def __init__(
        self,
        client: ClientSettings,
        credentials: Callable[[], Credentials],
        known: Callable[[], Sequence[Known]],
    ) -> None:
        self._client = client
        self._credentials = credentials
        self._known = known
        self._admin: Any = None
        self._errors: List[str] = []

    # -- plumbing -----------------------------------------------------------------

    def _fail(self, exc: BaseException, doing: str) -> FluxError:
        """The error to show for *exc*: scrubbed, one line, and never chained."""
        detail = _first_line(scrub(str(exc), self._known(), sink="kafka"))
        last = self._errors[-1] if self._errors else ""
        if not detail and last:
            detail = _first_line(scrub(last, self._known(), sink="kafka"))
        return FluxError(
            FLX_3505, f"kafka: {doing}: {type(exc).__name__}{': ' + detail if detail else ''}"
        )

    def _on_error(self, error: Any) -> None:
        self._errors.append(str(error))
        del self._errors[:-8]

    def _conf(self) -> Dict[str, Any]:
        client = self._client
        credentials = self._credentials()
        conf: Dict[str, Any] = {
            "bootstrap.servers": client.bootstrap,
            "logger": LOGGER,
            "error_cb": self._on_error,
        }
        if client.security_protocol:
            conf["security.protocol"] = client.security_protocol
        if client.sasl_mechanism:
            conf["sasl.mechanism"] = client.sasl_mechanism
        if credentials.sasl_username is not None and credentials.sasl_password is not None:
            conf["sasl.username"] = credentials.sasl_username
            conf["sasl.password"] = credentials.sasl_password
        if client.ssl_ca_location:
            conf["ssl.ca.location"] = client.ssl_ca_location
        return conf

    def _admin_client(self) -> Any:
        if self._admin is None:
            from confluent_kafka.admin import AdminClient

            self._admin = AdminClient(self._conf())
        return self._admin

    # -- Broker -------------------------------------------------------------------

    def describe(self, topic: str) -> Optional[TopicInfo]:
        """The topic's partitions and effective configuration, or ``None`` when it is absent."""
        from confluent_kafka import KafkaError
        from confluent_kafka.admin import ConfigResource

        timeout = self._client.timeout
        try:
            admin = self._admin_client()
            found = admin.list_topics(topic=topic, timeout=timeout).topics.get(topic)
            if found is None:
                return None
            if found.error is not None:
                if found.error.code() == KafkaError.UNKNOWN_TOPIC_OR_PART:
                    return None
                raise RuntimeError(str(found.error))
            resource = ConfigResource(ConfigResource.Type.TOPIC, topic)
            entries = admin.describe_configs([resource])[resource].result(timeout)
            configs = {
                name: str(entries[name].value)
                for name in TOPIC_CONFIGS
                if name in entries and entries[name].value is not None
            }
            return TopicInfo(partitions=len(found.partitions), configs=configs)
        except FluxError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._fail(exc, "could not describe the topic") from None

    def create(
        self,
        topic: str,
        *,
        partitions: Optional[int],
        replication_factor: Optional[int],
        configs: Mapping[str, str],
    ) -> TopicInfo:
        """Create *topic* with the brokers' defaults for what is ``None``, then describe it."""
        from confluent_kafka import KafkaError, KafkaException
        from confluent_kafka.admin import NewTopic

        timeout = self._client.timeout
        try:
            admin = self._admin_client()
            wanted = NewTopic(
                topic,
                num_partitions=partitions if partitions is not None else -1,
                replication_factor=replication_factor if replication_factor is not None else -1,
                config=dict(configs),
            )
            future = admin.create_topics(
                [wanted], operation_timeout=timeout, request_timeout=timeout
            )[topic]
            try:
                future.result(timeout)
            except KafkaException as exc:
                if exc.args[0].code() != KafkaError.TOPIC_ALREADY_EXISTS:
                    raise
            deadline = time.monotonic() + timeout
            while True:
                info = self.describe(topic)
                if info is not None:
                    return info
                if time.monotonic() >= deadline:
                    raise RuntimeError("the topic was created and is not yet visible")
                time.sleep(0.2)
        except FluxError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._fail(exc, "could not create the topic") from None

    def progress(self, group: str, topic: str) -> Optional[str]:
        """The metadata *group* committed on partition 0 of *topic*, read committed."""
        from confluent_kafka import Consumer, TopicPartition

        consumer = None
        try:
            consumer = Consumer(
                {
                    **self._conf(),
                    "group.id": group,
                    "enable.auto.commit": False,
                    "isolation.level": "read_committed",
                }
            )
            found = consumer.committed([TopicPartition(topic, 0)], timeout=self._client.timeout)
            if not found or found[0].error is not None or found[0].offset < 0:
                return None
            metadata = found[0].metadata
            return metadata if metadata else None
        except FluxError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._fail(exc, "could not read the stream's progress") from None
        finally:
            if consumer is not None:
                try:
                    consumer.close()
                except Exception:  # noqa: BLE001
                    LOGGER.debug("closing the progress consumer failed", exc_info=False)

    def transactional_producer(self, transactional_id: str, group: str) -> TxnProducer:
        """An initialised transactional producer; fences the previous one of the same id."""
        from confluent_kafka import Consumer, Producer

        timeout = self._client.timeout
        producer: Any = None
        consumer: Any = None
        try:
            producer = Producer(
                {
                    **self._conf(),
                    "enable.idempotence": True,
                    "transactional.id": transactional_id,
                    "partitioner": PARTITIONER,
                    # At least a minute, and never shorter than the time one call may take.
                    "transaction.timeout.ms": max(60_000, int(timeout * 1000)),
                }
            )
            producer.init_transactions(timeout)
            consumer = Consumer({**self._conf(), "group.id": group, "enable.auto.commit": False})
            return _ConfluentTxn(producer, consumer, self)
        except FluxError:
            raise
        except Exception as exc:  # noqa: BLE001
            if consumer is not None:
                try:
                    consumer.close()
                except Exception:  # noqa: BLE001
                    LOGGER.debug("closing the group consumer failed", exc_info=False)
            raise self._fail(exc, "could not open a transactional producer") from None

    def versions(self) -> Mapping[str, str]:
        """``confluent-kafka`` and ``librdkafka`` versions."""
        import confluent_kafka

        return {
            "confluentKafka": str(confluent_kafka.version()[0]),
            "librdkafka": str(confluent_kafka.libversion()[0]),
        }

    def close(self) -> None:
        """Drop the admin client; the next call opens a new one."""
        self._admin = None


class _ConfluentTxn:
    """:class:`TxnProducer` over one ``Producer`` and the group consumer that names its offsets."""

    def __init__(self, producer: Any, consumer: Any, owner: ConfluentBroker) -> None:
        self._producer = producer
        self._consumer = consumer
        self._owner = owner
        self._timeout = owner._client.timeout
        self._failures: List[str] = []

    def _delivered(self, error: Any, _message: Any) -> None:
        if error is not None:
            self._failures.append(str(error))

    def _raise_failures(self) -> None:
        if self._failures:
            raise self._owner._fail(
                RuntimeError(self._failures[0]), "a record was not acknowledged"
            ) from None

    def begin(self) -> None:
        try:
            self._producer.begin_transaction()
        except Exception as exc:  # noqa: BLE001
            raise self._owner._fail(exc, "could not begin a transaction") from None

    def produce(
        self,
        topic: str,
        key: bytes,
        value: Optional[bytes],
        timestamp_ms: int,
        headers: Sequence[Tuple[str, bytes]],
    ) -> None:
        self._raise_failures()
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                self._producer.produce(
                    topic,
                    key=key,
                    value=value,
                    timestamp=timestamp_ms,
                    headers=list(headers),
                    on_delivery=self._delivered,
                )
                break
            except BufferError:
                # The local queue is full: let deliveries drain, then try again.
                self._producer.poll(1)
                if time.monotonic() >= deadline:
                    raise self._owner._fail(
                        BufferError("the local queue stayed full"), "could not queue a record"
                    ) from None
            except Exception as exc:  # noqa: BLE001
                raise self._owner._fail(exc, "could not queue a record") from None
        self._producer.poll(0)

    def commit(self, *, topic: str, records: int, metadata: str) -> None:
        from confluent_kafka import TopicPartition

        try:
            if self._producer.flush(self._timeout) > 0:
                raise TimeoutError("records were still unacknowledged at the timeout")
            self._raise_failures()
            self._producer.send_offsets_to_transaction(
                [TopicPartition(topic, 0, records, metadata)],
                self._consumer.consumer_group_metadata(),
                self._timeout,
            )
            self._producer.commit_transaction(self._timeout)
        except FluxError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._owner._fail(exc, "could not commit the transaction") from None

    def abort(self) -> None:
        try:
            self._producer.abort_transaction(self._timeout)
        except Exception as exc:  # noqa: BLE001
            raise self._owner._fail(exc, "could not abort the transaction") from None

    def close(self) -> None:
        try:
            self._consumer.close()
        except Exception:  # noqa: BLE001
            LOGGER.debug("closing the group consumer failed", exc_info=False)
        self._producer = None
