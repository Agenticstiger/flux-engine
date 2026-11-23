# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The database sink: one stream into one table, through any ADBC driver.

ADBC (Arrow Database Connectivity) moves Arrow batches into a database
with the database's own bulk path: ``COPY`` in binary on PostgreSQL, a
Parquet stage and ``COPY INTO`` on Snowflake, the native appender on
DuckDB. One sink therefore covers SQLite, DuckDB, PostgreSQL, Snowflake and
BigQuery; the user installs the driver for their database next to
``flux-sim-engine[adbc]``.

**Where the rows go.** The table is ``<prefix><exposeId>`` (``sim_`` unless
told otherwise, with ``.`` and ``-`` in the expose id written as ``_``),
optionally in a schema, which is created when missing. There is no default
database: the sink needs an explicit ``uri`` (or, for BigQuery, explicit
driver options) and refuses to start without one.

**What every row carries.** After the stream's declared columns come four of
the sink's own: ``_flux_synthetic`` (always true), ``_flux_run_id``,
``_flux_output_digest`` and ``_flux_row``, the row's position in the
stream. The first three label every row as synthetic and say which run and
stream it came from; the last keeps the stream's order, so
``SELECT ... WHERE _flux_output_digest = '<digest>' ORDER BY _flux_row``
returns the stream exactly, and :func:`flux_engine.sinks.arrow.output_digest_of`
recomputes its digest from what the database holds. Where the database has
table comments (DuckDB, PostgreSQL, Snowflake) or descriptions (BigQuery),
the table is labelled too: a JSON comment with ``"synthetic": true``, the
expose, the run and the ``outputDigest`` of the latest load. SQLite has
neither, and its rows' own columns are the label.

**Replays replace their own slice.** In the default ``append`` mode a load
first deletes the rows whose ``_flux_output_digest`` is this stream's
digest, then inserts the stream, in one transaction. Re-running a
simulation therefore never duplicates its rows, and runs with other seeds
accumulate beside it, each in its own slice -- the delete-insert pattern
dlt's merge disposition uses, keyed on the ``outputDigest`` rather than a
business key. ``create`` refuses any existing table; ``replace`` drops and
recreates one the sink manages.

**The sink only touches its own tables.** An existing table without the
four ``_flux_`` columns is refused (``FLX-3506``) in every mode, so a
production table with a colliding name is never appended to, emptied or
dropped. In ``append`` mode the existing columns must also be this stream's
columns, in order.

**Exact or refused.** DECIMAL goes in as an Arrow decimal up to 38 digits,
the widest most databases hold exactly, and as its rendered text above
that. SQLite has no decimal and its driver writes timestamps without the
``Z``, so there DECIMAL and TIMESTAMP are stored as their rendered text,
which SQLite's own date functions read.

**Transactions.** On SQLite, DuckDB and PostgreSQL the whole load -- schema,
delete, insert, label -- is one transaction that a failure, or a stream
that stops short, rolls back. Snowflake commits DDL implicitly and BigQuery
has no transaction here, so on those a failed first load can leave an
empty table behind; the next run replaces it.

**Credentials never leave the connection.** The receipt records the target
without user, password, query string or fragment, and driver error
messages are scrubbed of the URI and its secrets before they are raised.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

import numpy

from flux_engine.api.emit import EmitHeader
from flux_engine.core.credentials import credential_dsn
from flux_engine.core.digest import digest_of_bytes
from flux_engine.core.errors import FluxError
from flux_engine.core.target_label import target_label
from flux_engine.seam.contract import LogicalKind
from flux_engine.seam.emit import FLX_3301
from flux_engine.sinks._columnar import positive_int
from flux_engine.sinks._optional import require
from flux_engine.sinks._options import (
    bool_option,
    int_option,
    option_name,
    require_known,
    text_option,
)
from flux_engine.sinks._secrets import Known, scrub
from flux_engine.sinks.arrow import DEFAULT_BATCH_SIZE, BatchBuilder, output_digest_of, pa
from flux_engine.sinks.errors import FLX_3501, FLX_3504, FLX_3505, FLX_3506
from flux_engine.sinks.receipts import SinkReceipt, StreamContext

if TYPE_CHECKING:  # pragma: no cover - annotations only
    import pyarrow

dbapi = require("adbc_driver_manager.dbapi", extra="adbc", feature="the database sink")

__all__ = [
    "DEFAULT_PREFIX",
    "DIALECTS",
    "MODES",
    "PROVENANCE_COLUMNS",
    "OPTIONS",
    "AdbcSink",
    "AdbcTarget",
    "Dialect",
    "load_driver",
    "table_name",
    "target",
]

#: The options ``--sink-opt`` may set, as the command line spells them, plus
#: any ``db.<key>`` driver option.
OPTIONS = ("batch-size", "mode", "prefix", "schema", "uri", "uri-env", "verify")

#: The twin namespace a table lands in unless the user names another.
DEFAULT_PREFIX = "sim_"

MODES = ("append", "create", "replace")

#: The sink's own columns, after the stream's, in this order.
PROVENANCE_COLUMNS = ("_flux_synthetic", "_flux_run_id", "_flux_output_digest", "_flux_row")

#: The widest decimal the supported databases hold exactly. Wider DECIMAL
#: columns go in as their rendered text.
_DECIMAL_TEXT_ABOVE = 38

#: PostgreSQL truncates longer identifiers silently; refuse them instead.
_MAX_IDENTIFIER = 63

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
#: Characters a column name may not hold on its way to a database.
_UNSAFE_IN_NAME = re.compile(r'["`\\\x00-\x1f\x7f]')

#: What a table label may contain. Every field of it is a digest, an
#: identifier or a version, so a quote or a backslash means something
#: upstream went wrong, and the label is refused rather than escaped.
_LABEL = re.compile(r'^[A-Za-z0-9_.:,{}" -]+$')


@dataclass(frozen=True)
class Dialect:
    """What the sink needs to know about one database."""

    name: str
    module: str
    package: str
    quote: str = '"'
    #: How a table is labelled: ``comment-on``, ``options`` (BigQuery), or ``""``.
    label: str = "comment-on"
    text_kinds: FrozenSet[LogicalKind] = frozenset()
    #: Whether the driver takes a connection URI (BigQuery takes options).
    takes_uri: bool = True
    #: Whether the driver accepts ``db.*`` options beside the URI.
    takes_options: bool = True
    has_schemas: bool = True
    #: Whether a schema-qualified name also needs its catalog. DuckDB names a
    #: catalog after the database file, so ``sim.duckdb`` with schema ``sim``
    #: makes ``"sim"."t"`` ambiguous; ``"sim"."sim"."t"`` is not.
    qualify_catalog: bool = False
    #: The oldest release of *package* whose driver does what the sink needs.
    min_version: Tuple[int, ...] = ()

    def quoted(self, identifier: str) -> str:
        """*identifier* quoted, after checking it against the identifier grammar.

        Only the sink's own names reach SQL text this way: the table, the
        schema and the ``_flux_`` columns, each checked when it is made. The
        check is repeated here so that no other string can ever be spliced
        into a statement through this method: a stream's column names come
        from a contract the user may have received from someone else, and
        they never appear in SQL the sink writes (the driver quotes them
        when it creates the table).
        """
        if not isinstance(identifier, str) or not _IDENTIFIER.fullmatch(identifier):
            raise FluxError(FLX_3504, "the name is not an identifier the sink will quote")
        return f"{self.quote}{identifier}{self.quote}"

    def quoted_name(self, name: str) -> str:
        """A name the database chose (a catalog), quoted with its quotes doubled."""
        return f"{self.quote}{name.replace(self.quote, self.quote * 2)}{self.quote}"


DIALECTS: Mapping[str, Dialect] = {
    "sqlite": Dialect(
        name="sqlite",
        module="adbc_driver_sqlite.dbapi",
        package="adbc-driver-sqlite",
        label="",
        text_kinds=frozenset({LogicalKind.DECIMAL, LogicalKind.TIMESTAMP}),
        takes_options=False,
        has_schemas=False,
    ),
    "duckdb": Dialect(
        name="duckdb",
        module="adbc_driver_duckdb.dbapi",
        package="duckdb",
        takes_options=False,
        qualify_catalog=True,
        # Older drivers refuse the replace and create_append ingest modes.
        min_version=(1, 5),
    ),
    "postgresql": Dialect(
        name="postgresql", module="adbc_driver_postgresql.dbapi", package="adbc-driver-postgresql"
    ),
    "snowflake": Dialect(
        name="snowflake", module="adbc_driver_snowflake.dbapi", package="adbc-driver-snowflake"
    ),
    "bigquery": Dialect(
        name="bigquery",
        module="adbc_driver_bigquery.dbapi",
        package="adbc-driver-bigquery",
        quote="`",
        label="options",
        takes_uri=False,
    ),
}


def _identifier(what: str, value: str, *, empty: bool = False) -> str:
    if (empty and value == "") or (isinstance(value, str) and _IDENTIFIER.fullmatch(value)):
        if len(value) > _MAX_IDENTIFIER:
            raise FluxError(FLX_3504, f"{what} is longer than {_MAX_IDENTIFIER} characters")
        return value
    raise FluxError(
        FLX_3504,
        f"{what} must be letters, digits and underscores, not starting with a digit",
    )


def table_name(prefix: str, expose_id: str) -> str:
    """``<prefix><exposeId>``, with the dots and hyphens FLUID allows written as ``_``."""
    return _identifier("table name", prefix + expose_id.replace(".", "_").replace("-", "_"))


def _known_strings(
    uri: Optional[str], options: Mapping[str, str], uri_env: Optional[str]
) -> List[Known]:
    """The strings of the target a driver may echo in a message: the uri (written as ``$NAME`` when a variable holds it) and each driver option's value."""
    known: List[Known] = [text for text in options.values() if text]
    if uri:
        known.append((uri, f"${uri_env}") if uri_env else uri)
    return known


class AdbcSink:
    """Writes one stream into one table through an ADBC driver.

    Parameters
    ----------
    driver
        ``sqlite``, ``duckdb``, ``postgresql``, ``snowflake`` or ``bigquery``.
    context
        The stream's run, expose, row count and ``outputDigest``.
    uri
        The connection URI, or the database file for SQLite and DuckDB.
        Required except for BigQuery, which takes *db_options*.
    db_options
        Driver options passed through as ADBC database options, such as
        ``adbc.bigquery.sql.project_id``.
    schema
        The schema (BigQuery: dataset) to write into; created when missing.
        Not available on SQLite.
    prefix
        The table name prefix, ``sim_`` by default. May be empty.
    mode
        ``append`` (default), ``create`` or ``replace``.
    batch_size
        Rows per Arrow batch handed to the driver.
    verify
        Read the slice back inside the transaction and recompute the
        ``outputDigest`` before committing.
    """

    name: ClassVar[str] = "adbc"

    def __init__(
        self,
        driver: str,
        context: StreamContext,
        *,
        uri: Optional[str] = None,
        db_options: Optional[Mapping[str, str]] = None,
        schema: Optional[str] = None,
        prefix: str = DEFAULT_PREFIX,
        mode: str = "append",
        batch_size: int = DEFAULT_BATCH_SIZE,
        verify: bool = False,
        uri_env: Optional[str] = None,
    ) -> None:
        if driver not in DIALECTS:
            raise FluxError(
                FLX_3504,
                f"driver {target_label(self.name, driver)!r} is not one of "
                f"{', '.join(sorted(DIALECTS))}",
            )
        self.dialect = DIALECTS[driver]
        self.context = context
        options = dict(db_options or {})
        if options and not self.dialect.takes_options:
            raise FluxError(FLX_3504, f"the {driver} driver takes no db options; pass uri alone")
        if self.dialect.takes_uri and not uri:
            raise FluxError(
                FLX_3504,
                f"the {driver} driver needs an explicit uri: the sink has no default database",
            )
        if not self.dialect.takes_uri and (uri or not options):
            raise FluxError(
                FLX_3504,
                f"the {driver} driver takes db options (its project and dataset), not a uri",
            )
        if mode not in MODES:
            raise FluxError(FLX_3504, f"mode is not one of {', '.join(MODES)}")
        if schema is not None:
            if not self.dialect.has_schemas:
                raise FluxError(FLX_3504, f"the {driver} driver has no schemas; use a prefix")
            _identifier("schema", schema)
        if not _DIGEST.fullmatch(context.output_digest.value):
            raise FluxError(FLX_3504, "the stream's outputDigest is not a sha256 digest")
        if not isinstance(verify, bool):
            raise FluxError(FLX_3504, "verify must be true or false")
        self.uri = uri
        self.uri_env = uri_env
        self.db_options = options
        self.schema = schema
        self.prefix = _identifier("prefix", prefix, empty=True)
        self.table = table_name(self.prefix, context.expose_id)
        self.mode = mode
        self.batch_size = positive_int("batch-size", batch_size)
        self.verify = verify
        self._known = _known_strings(uri, options, uri_env)
        self._header: Optional[EmitHeader] = None
        self._builder: Optional[BatchBuilder] = None
        self._schema: Optional["pyarrow.Schema"] = None
        self._connection: Any = None
        self._cursor: Any = None
        self._next_mode = "create"
        self._ingested = 0
        self._closed = False
        self._failed = False
        self._driver_version = ""
        self._catalog: Optional[str] = None
        self.receipt: Optional[SinkReceipt] = None

    # -- names and SQL ---------------------------------------------------------

    @property
    def qualified_table(self) -> str:
        """The table's name as SQL spells it, schema included."""
        quoted = self.dialect.quoted(self.table)
        if self.schema is None:
            return quoted
        qualified = f"{self.dialect.quoted(self.schema)}.{quoted}"
        if self._catalog:
            return f"{self.dialect.quoted_name(self._catalog)}.{qualified}"
        return qualified

    @property
    def target(self) -> str:
        """Where the rows go, as a label of its non-secret parts.

        A URI read from an environment variable is shown as the variable's name
        (``sqlite:$SIM_URI``): its value is the one thing never printed. Any other
        is :func:`~flux_engine.core.target_label.target_label` of it: a URL
        keeps its scheme, host, port and path (``postgresql://…@db:5432/sim?…``);
        a connection string keeps its host and database pairs. A file or a scheme-less
        location gets the driver in front (``duckdb:sim.duckdb``,
        ``snowflake:account/db/schema``).
        """
        if not self.uri:
            return self.dialect.name
        if self.uri_env:
            return f"{self.dialect.name}:${self.uri_env}"
        shown = target_label(self.dialect.name, self.uri)
        if "://" in shown or "=" in shown or " " in shown:
            return shown
        return f"{self.dialect.name}:{shown}"

    def _label(self) -> str:
        from flux_engine import __version__

        label = json.dumps(
            {
                "synthetic": True,
                "generator": f"flux-sim-engine {__version__}",
                "exposeId": self.context.expose_id,
                "runId": self.context.run_id,
                "outputDigest": self.context.output_digest.value,
            },
            sort_keys=True,
        )
        if not _LABEL.fullmatch(label):
            raise FluxError(FLX_3504, "the table label holds characters a SQL literal must escape")
        return label

    def _execute(self, sql: str) -> None:
        self._cursor.execute(sql)

    def _fail(self, exc: BaseException, doing: str) -> FluxError:
        # Scrub the whole message, then take its first line: a value that ends in a line break is
        # echoed without it, and the first line would no longer match.
        lines = scrub(str(exc), self._known, sink=self.dialect.name).strip().splitlines()
        detail = (lines or [type(exc).__name__])[0][:300]
        return FluxError(FLX_3505, f"{self.target}: {doing}: {type(exc).__name__}: {detail}")

    # -- the EmitSink protocol --------------------------------------------------

    def open(self, header: EmitHeader) -> None:
        """Check the stream, connect, and prepare the table for this slice."""
        if self._header is not None:
            raise FluxError(FLX_3301, f"expose {header.expose_id!r}: sink already open")
        if self._closed:
            raise FluxError(FLX_3301, f"expose {header.expose_id!r}: sink is closed")
        if header.expose_id != self.context.expose_id:
            raise FluxError(
                FLX_3504,
                f"this database sink was set up for expose {self.context.expose_id!r} "
                f"and opened on {header.expose_id!r}",
            )
        clashes = [name for name in header.columns if name.startswith("_flux_")]
        if clashes:
            raise FluxError(
                FLX_3504,
                f"columns {clashes} use the _flux_ prefix the database sink keeps for itself",
            )
        unsafe = [name for name in header.columns if _UNSAFE_IN_NAME.search(name)]
        if unsafe:
            # FLUID allows any column name, and the driver quotes it when it
            # creates the table. A quote, a backslash or a control character
            # is where quoting has to be right, so the sink does not hand one
            # to a driver at all.
            raise FluxError(
                FLX_3504,
                f"columns {unsafe} hold a quote, a backslash or a control character, which "
                "the database sink will not send to a database; rename them in the FLUID "
                "contract",
            )
        self._header = header
        self._builder = BatchBuilder(
            header,
            batch_size=self.batch_size,
            text_kinds=self.dialect.text_kinds,
            decimal_text_above=_DECIMAL_TEXT_ABOVE,
        )
        self._schema = self._builder.schema
        for name, arrow_type in zip(
            PROVENANCE_COLUMNS, (pa.bool_(), pa.string(), pa.string(), pa.int64()), strict=True
        ):
            self._schema = self._schema.append(pa.field(name, arrow_type, nullable=False))
        driver = load_driver(self.dialect)
        try:
            if not self.dialect.takes_uri:
                self._connection = driver.connect(db_kwargs=dict(self.db_options))
            elif self.dialect.takes_options:
                self._connection = driver.connect(self.uri, db_kwargs=dict(self.db_options) or None)
            else:
                self._connection = driver.connect(self.uri)
            self._cursor = self._connection.cursor()
            info = self._connection.adbc_get_info()
            self._driver_version = str(info.get("driver_version") or "")
            if self.dialect.qualify_catalog and self.schema is not None:
                self._catalog = self._connection.adbc_current_catalog or None
        except dbapi.Error as exc:
            self._release()
            raise self._fail(exc, "could not connect") from exc
        try:
            self._prepare()
        except FluxError:
            self._rollback()
            self._release()
            raise
        except dbapi.Error as exc:
            self._rollback()
            self._release()
            raise self._fail(exc, f"could not prepare {self.qualified_table}") from exc

    def _existing_columns(self) -> Optional[List[str]]:
        """The table's column names, or ``None`` when there is no such table."""
        try:
            schema = self._connection.adbc_get_table_schema(
                self.table, catalog_filter=self._catalog, db_schema_filter=self.schema
            )
        except dbapi.Error:
            # Absent (or unreadable). A failed statement aborts the transaction
            # on some databases, so start clean; a table that does exist after
            # all then fails the create below rather than being written over.
            self._connection.rollback()
            return None
        return list(schema.names)

    def _prepare(self) -> None:
        assert self._schema is not None
        # Look before creating anything: a failed lookup rolls the transaction
        # back, and with it any schema created earlier in it.
        existing = self._existing_columns()
        if existing is None:
            if self.schema is not None:
                self._execute(f"CREATE SCHEMA IF NOT EXISTS {self.dialect.quoted(self.schema)}")
            self._next_mode = "create"
            return
        if self.mode == "create":
            raise FluxError(
                FLX_3506, f"{self.qualified_table} exists, and mode=create never touches a table"
            )
        if list(existing[-len(PROVENANCE_COLUMNS) :]) != list(PROVENANCE_COLUMNS):
            raise FluxError(
                FLX_3506,
                f"{self.qualified_table} exists and was not created by the database sink "
                f"(it lacks the {', '.join(PROVENANCE_COLUMNS)} columns)",
            )
        if self.mode == "replace":
            self._next_mode = "replace"
            return
        expected = list(self._schema.names)
        if existing != expected:
            raise FluxError(
                FLX_3506,
                f"{self.qualified_table} has columns {existing}, this stream has {expected}; "
                "use mode=replace to rebuild it",
            )
        self._execute(
            f"DELETE FROM {self.qualified_table} "
            f"WHERE {self.dialect.quoted('_flux_output_digest')} = "
            f"'{self.context.output_digest.value}'"
        )
        self._next_mode = "append"

    def write(self, row: Sequence[object]) -> None:
        """Add one rendered row; full batches go to the database as they fill."""
        if self._builder is None:
            raise FluxError(FLX_3301, "sink written to before open")
        if self._closed:
            raise FluxError(FLX_3301, f"expose {self.context.expose_id!r}: sink is closed")
        try:
            batch = self._builder.append(row)
            if batch is not None:
                self._ingest(batch)
        except BaseException:
            # The rows of a batch that failed were counted but never written:
            # remember it, so close() rolls back instead of committing them.
            self._failed = True
            raise

    def _with_provenance(self, batch: "pyarrow.RecordBatch") -> "pyarrow.RecordBatch":
        count = batch.num_rows
        start = self._ingested
        columns = list(batch.columns) + [
            pa.repeat(pa.scalar(True), count),
            pa.repeat(pa.scalar(self.context.run_id), count),
            pa.repeat(pa.scalar(self.context.output_digest.value), count),
            pa.array(numpy.arange(start, start + count, dtype=numpy.int64)),
        ]
        return pa.RecordBatch.from_arrays(columns, schema=self._schema)

    def _ingest(self, batch: "pyarrow.RecordBatch") -> None:
        try:
            self._cursor.adbc_ingest(
                self.table,
                self._with_provenance(batch),
                mode=self._next_mode,
                catalog_name=self._catalog,
                db_schema_name=self.schema,
            )
        except dbapi.Error as exc:
            raise self._fail(exc, f"could not write to {self.qualified_table}") from exc
        self._next_mode = "append"
        self._ingested += batch.num_rows

    def close(self) -> None:
        """Commit when the whole stream arrived; roll back otherwise."""
        if self._closed:
            return
        self._closed = True
        builder = self._builder
        if builder is None or self._connection is None:
            self._release()
            return
        try:
            if self._failed or builder.rows != self.context.rows:
                self._rollback()
                return
            tail = builder.flush()
            if tail is not None:
                self._ingest(tail)
            if self._next_mode != "append":
                # An empty stream still creates (or empties) its table.
                self._ingest(builder.empty_batch())
            self._apply_label()
            if self.verify:
                self._check_read_back(builder)
            self._connection.commit()
        except dbapi.Error as exc:
            self._rollback()
            raise self._fail(exc, f"could not finish {self.qualified_table}") from exc
        except BaseException:
            self._rollback()
            raise
        finally:
            self._release()
        self.receipt = SinkReceipt(
            sink=self.name,
            run_id=self.context.run_id,
            expose_id=self.context.expose_id,
            output_digest=self.context.output_digest.value,
            rows=builder.rows,
            target=self.target,
            table=self.table if self.schema is None else f"{self.schema}.{self.table}",
            writer={
                "library": "adbc-driver-manager",
                "version": _distribution_version("adbc-driver-manager"),
                "driver": self.dialect.name,
                "driverVersion": self._driver_version,
                "mode": self.mode,
                "batchSize": self.batch_size,
            },
        )

    def _apply_label(self) -> None:
        if not self.dialect.label:
            return
        label = self._label()
        if self.dialect.label == "options":
            self._execute(
                f"ALTER TABLE {self.qualified_table} SET OPTIONS (description = '{label}')"
            )
        else:
            self._execute(f"COMMENT ON TABLE {self.qualified_table} IS '{label}'")

    def _check_read_back(self, builder: BatchBuilder) -> None:
        # SELECT *, then pick the stream's columns by position: the stream's
        # own column names are never written into a statement.
        self._execute(
            f"SELECT * FROM {self.qualified_table} "
            f"WHERE {self.dialect.quoted('_flux_output_digest')} = "
            f"'{self.context.output_digest.value}' "
            f"ORDER BY {self.dialect.quoted('_flux_row')}"
        )
        table = self._cursor.fetch_arrow_table()
        count = len(builder.header.columns)
        if list(table.schema.names[:count]) != list(builder.header.columns):
            raise FluxError(
                FLX_3505,
                f"{self.qualified_table} does not start with the stream's columns",
            )
        found = output_digest_of(table.select(list(range(count))), header=builder.header)
        if found.value != self.context.output_digest.value:
            raise FluxError(
                FLX_3505,
                f"{self.qualified_table} holds a slice that digests to {found.value}, "
                f"not to the stream's {self.context.output_digest.value}",
            )

    def _rollback(self) -> None:
        if self._connection is None:
            return
        try:
            self._connection.rollback()
        except Exception:  # pragma: no cover - the connection is being dropped
            pass

    def _release(self) -> None:
        for handle in (self._cursor, self._connection):
            if handle is None:
                continue
            try:
                handle.close()
            except Exception:  # pragma: no cover - best effort on the way out
                pass
        self._cursor = None
        self._connection = None


def load_driver(dialect: Dialect) -> Any:
    """Import *dialect*'s ADBC driver, or ``FLX-3501`` naming what to install."""
    driver = require(
        dialect.module,
        extra="",
        feature=f"the database sink's {dialect.name} driver",
        hint=f"pip install 'flux-sim-engine[adbc]' {dialect.package}",
    )
    wanted = dialect.min_version
    if wanted:
        installed = _distribution_version(dialect.package)
        parts = tuple(int(part) for part in re.findall(r"\d+", installed)[: len(wanted)])
        if parts < wanted:
            floor = ".".join(str(part) for part in wanted)
            raise FluxError(
                FLX_3501,
                f"the database sink's {dialect.name} driver needs {dialect.package} "
                f"{floor} or later, and {installed or 'an unknown version'} is installed: "
                f"pip install '{dialect.package}>={floor}'",
            )
    return driver


def _distribution_version(name: str) -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(name)
    except PackageNotFoundError:  # pragma: no cover - imported, so installed
        return ""


#: Driver option names that carry a secret.
_SECRET_KEY = re.compile(r"pass|secret|token|key|credential", re.IGNORECASE)


def _carries_secret(uri: str) -> bool:
    """Whether *uri* holds a secret inline, in any DSN form (see :func:`credential_dsn`)."""
    return credential_dsn(uri)


@dataclass(frozen=True)
class AdbcTarget:
    """The database sink's checked settings, handing out one sink per stream."""

    driver: str
    # Kept out of the repr: either may hold a credential read from the
    # environment, and a repr ends up in logs and tracebacks.
    uri: Optional[str] = field(default=None, repr=False)
    db_options: Mapping[str, str] = field(default_factory=dict, repr=False)
    schema: Optional[str] = None
    prefix: str = DEFAULT_PREFIX
    mode: str = "append"
    batch_size: int = DEFAULT_BATCH_SIZE
    verify: bool = False
    uri_env: Optional[str] = None
    name: str = "adbc"
    _tables: Dict[str, str] = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        probe = StreamContext(
            run_id="probe",
            expose_id="probe",
            rows=0,
            output_digest=digest_of_bytes(b"", subject="probe"),
        )
        # Every setting, the driver and its version are checked now, before
        # the simulation runs; nothing connects yet.
        self._sink(probe)
        load_driver(DIALECTS[self.driver])

    def sink_for(self, context: StreamContext) -> AdbcSink:
        """A fresh sink for one stream; refuses a second expose in the same table."""
        sink = self._sink(context)
        if context.expose_id != "probe" or context.run_id != "probe":
            # Case-folded: SQLite and DuckDB match table names without regard
            # to case, so sim_Pay and sim_pay are one table there.
            other = self._tables.setdefault(sink.qualified_table.casefold(), context.expose_id)
            if other != context.expose_id:
                raise FluxError(
                    FLX_3504,
                    f"exposes {other!r} and {context.expose_id!r} would both land in "
                    f"{sink.qualified_table}; give them distinct ids or deliver them apart",
                )
        return sink

    def _sink(self, context: StreamContext) -> AdbcSink:
        return AdbcSink(
            self.driver,
            context,
            uri=self.uri,
            db_options=self.db_options,
            schema=self.schema,
            prefix=self.prefix,
            mode=self.mode,
            batch_size=self.batch_size,
            verify=self.verify,
            uri_env=self.uri_env,
        )


def _driver_options(options: Mapping[str, str]) -> Dict[str, str]:
    """``db.KEY=VALUE`` options, with ``db.KEY-env=VAR`` read from the environment.

    A key that names a secret (a password, a token, a private key) is
    refused as an inline value, for the reason the uri is: the command line
    is the wrong place for it. ``db.KEY-env=VAR`` passes it instead.
    """
    driver_options: Dict[str, str] = {}
    for key, value in options.items():
        if not key.startswith("db."):
            continue
        name = key[3:]
        if name.endswith("-env"):
            name = name[: -len("-env")]
            resolved = os.environ.get(value)
            if not resolved:
                raise FluxError(
                    FLX_3504,
                    f"the environment variable that db.{option_name(name)}-env names is not set",
                )
            driver_options[name] = resolved
            continue
        if _SECRET_KEY.search(name):
            raise FluxError(
                FLX_3504,
                f"the driver option {option_name(name)} looks like a secret, and a command line is not the "
                f"place for one: pass --sink-opt db.{option_name(name)}-env=NAME instead",
            )
        driver_options[name] = value
    return driver_options


def target(driver: str, options: Mapping[str, str]) -> AdbcTarget:
    """The ``adbc`` sink's factory: ``--sink adbc:DRIVER`` and its options.

    The connection comes from ``uri`` -- a database file, or a URI without
    a password -- or from ``uri-env``, the name of an environment variable
    holding the URI. A URI with a password or a secret query parameter is
    refused as an option, because a command line is visible to every user
    of the machine and is kept in shell history; ``uri-env`` is the way to
    pass one. ``db.<key>-env=<VAR>`` reads a driver option from the
    environment, the only way to pass one that names a secret.
    ``db.<key>=<value>`` options go to the driver as ADBC
    database options.
    """
    if not driver:
        raise FluxError(
            FLX_3504,
            "the adbc sink needs a driver: --sink adbc:DRIVER, one of "
            + ", ".join(sorted(DIALECTS)),
        )
    db_options = _driver_options(options)
    plain = {key: value for key, value in options.items() if not key.startswith("db.")}
    require_known("adbc", plain, OPTIONS)
    uri = text_option(plain, "uri")
    variable = text_option(plain, "uri-env")
    if uri is not None and variable is not None:
        raise FluxError(FLX_3504, "give uri or uri-env, not both")
    if uri is not None and _carries_secret(uri):
        raise FluxError(
            FLX_3504,
            "the uri carries a password or a secret, and a command line is not the place "
            "for one: put the uri in an environment variable and pass --sink-opt uri-env=NAME",
        )
    if variable is not None:
        uri = os.environ.get(variable)
        if not uri:
            raise FluxError(FLX_3504, "the environment variable that uri-env names is not set")
    return AdbcTarget(
        driver=driver,
        uri=uri,
        db_options=db_options,
        schema=text_option(plain, "schema"),
        prefix=text_option(plain, "prefix", DEFAULT_PREFIX) or "",
        mode=text_option(plain, "mode", "append") or "append",
        batch_size=int_option(plain, "batch-size", DEFAULT_BATCH_SIZE) or DEFAULT_BATCH_SIZE,
        verify=bool_option(plain, "verify", False),
        uri_env=variable,
    )
