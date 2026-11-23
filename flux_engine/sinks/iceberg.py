# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The Iceberg sink: one stream as one Iceberg table, through pyiceberg.

``iceberg:<catalog>/<namespace>[/<level>...]`` writes the table of a stream into the
catalogue named ``<catalog>``, in the namespace the levels name. The catalogue name and
each level are checked with the rules of an object prefix's segments (``FLX-3510``): no
empty segment, ``.``, ``..``, backslash or percent-encoded separator. At least one level
is required.

**The table is named by a tuple**, ``(*namespace_levels, exposeId)``, never by a dotted
string, so an ``exposeId`` that holds a dot stays one table name and never becomes a
level of the namespace.

**Configuration and credentials are never passed on.** The catalogue is loaded with
``pyiceberg.catalog.load_catalog(<catalog name>)`` and no property, so its configuration
and credentials come only from the person's own ``.pyiceberg.yaml`` in the home directory
or in ``PYICEBERG_HOME``, and from ``PYICEBERG_*`` environment variables. pyiceberg also
reads a ``.pyiceberg.yaml`` in the current directory, which could point the catalogue,
and the storage requests it signs, at another endpoint: the sink refuses to start when
one is there and the current directory is neither the home directory nor
``PYICEBERG_HOME`` (``FLX-3511``).

**Modes.** ``create`` (the default) creates the table from the stream's Arrow schema and
refuses one that exists. ``append`` and ``overwrite`` write into a table that exists and
refuse one that does not: pyiceberg has no natural append that creates. The namespace
must exist already; the sink never creates one.

The receipt records the target as ``iceberg:<catalog>/<namespace>`` (no URI, no
property), the table identifier as a list, the snapshot id the write made and the writer,
``pyiceberg <version>``.

Needs ``flux-sim-engine[publish]``; this module is imported only when the sink is selected.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Mapping, Tuple

from flux_engine.core.digest import digest_of_bytes
from flux_engine.core.errors import FluxError
from flux_engine.core.target_label import target_label
from flux_engine.seam.coverage import printable
from flux_engine.sinks._files import require_segment
from flux_engine.sinks._objects import require_plain_segment
from flux_engine.sinks._optional import require
from flux_engine.sinks._options import require_known, text_option
from flux_engine.sinks._tables import TableSink, TableWrite, require_mode
from flux_engine.sinks.errors import FLX_3504, FLX_3505, FLX_3511
from flux_engine.sinks.receipts import StreamContext

if TYPE_CHECKING:  # pragma: no cover
    import pyarrow

pyiceberg = require("pyiceberg", extra="publish", feature="the iceberg sink")
catalogs = require("pyiceberg.catalog", extra="publish", feature="the iceberg sink")

__all__ = [
    "CONFIG_FILE",
    "DEFAULT_MODE",
    "MODES",
    "OPTIONS",
    "IcebergSink",
    "IcebergTarget",
    "parse_target",
    "refuse_a_catalogue_file_in_the_current_directory",
    "target",
]

#: The options ``--sink-opt`` may set, as the command line spells them.
OPTIONS = ("mode",)

MODES = ("create", "append", "overwrite")
DEFAULT_MODE = "create"

#: The catalogue configuration file pyiceberg reads from several directories.
CONFIG_FILE = ".pyiceberg.yaml"

_SHAPE = "iceberg:CATALOG/NAMESPACE[/LEVEL...]"


def _same_directory(left: str, right: str) -> bool:
    return Path(left).resolve() == Path(right).resolve()


def refuse_a_catalogue_file_in_the_current_directory() -> None:
    """``FLX-3511`` when ``.pyiceberg.yaml`` is in the current directory and it is not a home.

    The homes pyiceberg reads are the home directory and ``PYICEBERG_HOME``: a file there
    is the person's own. Anywhere else, a file beside the run could be a bundle's.
    """
    try:
        here = os.getcwd()
    except OSError:
        return
    if not os.path.lexists(os.path.join(here, CONFIG_FILE)):
        return
    homes = [os.path.expanduser("~")]
    configured = os.environ.get("PYICEBERG_HOME")
    if configured:
        homes.append(configured)
    if any(_same_directory(here, home) for home in homes):
        return
    raise FluxError(
        FLX_3511,
        f"{CONFIG_FILE} is in the current directory ({printable(here)}), which is neither the home "
        "directory nor PYICEBERG_HOME; run from another directory, or keep the catalogue "
        "configuration in one of those two",
    )


def parse_target(text: str) -> Tuple[str, Tuple[str, ...]]:
    """Split ``CATALOG/NAMESPACE[/LEVEL...]`` into the catalogue name and the levels.

    Every segment is checked as an object prefix's is (``FLX-3510``); a target with no
    catalogue or no namespace level is ``FLX-3504``. A trailing ``/`` is dropped.
    """
    shown = target_label("iceberg", f"iceberg:{text}")
    path = text[:-1] if text.endswith("/") else text
    segments = path.split("/")
    if len(segments) < 2:
        raise FluxError(FLX_3504, f"{shown!r} names no namespace: {_SHAPE}")
    name, *levels = segments
    require_plain_segment(name, what="the catalogue name", shown=shown)
    for level in levels:
        require_plain_segment(level, what="a namespace level", shown=shown)
    return name, tuple(levels)


class IcebergSink(TableSink):
    """Writes one stream as the Iceberg table ``(*namespace, exposeId)`` of a catalogue.

    Parameters
    ----------
    catalog
        The catalogue's name, as pyiceberg's configuration names it.
    namespace
        The namespace's levels, at least one.
    context
        The stream's run, expose, row count and ``outputDigest``.
    mode
        ``create`` (default), ``append`` or ``overwrite``.
    """

    name = "iceberg"

    def __init__(
        self,
        catalog: str,
        namespace: Tuple[str, ...],
        context: StreamContext,
        *,
        mode: str = DEFAULT_MODE,
    ) -> None:
        super().__init__(context)
        if not namespace:
            raise FluxError(FLX_3504, f"the iceberg sink needs a namespace: {_SHAPE}")
        self.mode = require_mode(self.name, mode, MODES)
        self.catalog = catalog
        self.namespace = tuple(namespace)
        # A tuple, never a dotted string: an exposeId with a dot is still one name.
        self.identifier: Tuple[str, ...] = (*self.namespace, require_segment(context.expose_id))

    @property
    def target(self) -> str:
        return target_label(self.name, f"iceberg:{self.catalog}/{'/'.join(self.namespace)}")

    def _known_strings(self) -> List[str]:
        return [self.catalog, "/".join(self.namespace)]

    def _publish(self, table: "pyarrow.Table") -> TableWrite:
        refuse_a_catalogue_file_in_the_current_directory()
        # No property: the catalogue's configuration and credentials are the person's own.
        catalog: Any = catalogs.load_catalog(self.catalog)
        exists = bool(catalog.table_exists(self.identifier))
        if self.mode == "create":
            if exists:
                raise FluxError(
                    FLX_3505,
                    f"the table {printable('/'.join(self.identifier))} already exists in "
                    f"{printable(self.target)}; "
                    "mode=create refuses it, and mode=append or mode=overwrite writes into it",
                )
            # The table and its first snapshot are committed together, so a failed
            # write leaves no empty table behind.
            transaction = catalog.create_table_transaction(self.identifier, schema=table.schema)
            transaction.append(table)
            transaction.commit_transaction()
            written = catalog.load_table(self.identifier)
        else:
            if not exists:
                raise FluxError(
                    FLX_3505,
                    f"the table {printable('/'.join(self.identifier))} does not exist in "
                    f"{printable(self.target)}; "
                    f"mode={self.mode} writes into an existing table, and mode=create makes one",
                )
            written = catalog.load_table(self.identifier)
            if self.mode == "append":
                written.append(table)
            else:
                written.overwrite(table)
        snapshot = written.current_snapshot()
        return TableWrite(
            target=self.target,
            table=self.identifier,
            writer={
                "library": "pyiceberg",
                "version": pyiceberg.__version__,
                "writer": f"pyiceberg {pyiceberg.__version__}",
                "mode": self.mode,
                # Text: a snapshot id is a 64-bit integer, more than JSON readers keep exactly.
                "snapshotId": None if snapshot is None else str(snapshot.snapshot_id),
            },
        )


_PROBE = StreamContext(
    run_id="probe", expose_id="probe", rows=0, output_digest=digest_of_bytes(b"", subject="probe")
)


@dataclass(frozen=True)
class IcebergTarget:
    """The Iceberg sink's checked settings, handing out one sink per stream."""

    catalog: str
    namespace: Tuple[str, ...]
    mode: str = DEFAULT_MODE
    name: str = "iceberg"

    def __post_init__(self) -> None:
        # The names and the mode are checked now, before the simulation runs.
        self.sink_for(_PROBE)

    def sink_for(self, context: StreamContext) -> IcebergSink:
        """A fresh sink for one stream."""
        return IcebergSink(self.catalog, self.namespace, context, mode=self.mode)


def target(location: str, options: Mapping[str, str]) -> IcebergTarget:
    """The ``iceberg`` sink's factory: ``--sink iceberg:CATALOG/NAMESPACE`` and ``mode``."""
    require_known("iceberg", options, OPTIONS)
    refuse_a_catalogue_file_in_the_current_directory()
    name, namespace = parse_target(location)
    return IcebergTarget(
        catalog=name,
        namespace=namespace,
        mode=text_option(options, "mode", DEFAULT_MODE) or "",
    )
