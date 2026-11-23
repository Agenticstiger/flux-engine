# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The Delta sink: one stream as one Delta Lake table, through deltalake.

``delta:<target>`` writes the table of a stream at ``<target>/<exposeId>``. The target is
an object store URL (``s3://``, ``gs://``, ``az://`` or ``abfs://``), parsed and checked
as the file sinks' object targets are (``FLX-3510``), or a local directory, which is
resolved and refused when it is a symbolic link.

**Credentials and endpoints are never passed on.** The table is written with
``deltalake.write_deltalake(location, table, mode=...)`` and no ``storage_options``, so
deltalake reads them from the person's environment and the platform's own credential
chain. The sink's one option is ``mode``; nothing reaches deltalake through it.

**Modes.** ``error`` (the default) refuses a table that exists, ``append`` adds the
stream's rows to it (creating it when absent) and ``overwrite`` replaces its contents.

The receipt records the target as a label of its non-secret parts, the table's location, the
version the write made and the writer, ``deltalake <version>``.

Needs ``flux-sim-engine[publish]``; this module is imported only when the sink is selected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, List, Mapping, Optional, Union

from flux_engine.core.digest import digest_of_bytes
from flux_engine.core.errors import FluxError
from flux_engine.core.target_label import target_label
from flux_engine.safeio import require_real_directory
from flux_engine.sinks._files import require_segment
from flux_engine.sinks._objects import (
    ObjectLocation,
    instance_identity_option,
    is_object_target,
    parse_object_target,
    require_explicit_credentials,
)
from flux_engine.sinks._optional import require
from flux_engine.sinks._options import require_known, text_option
from flux_engine.sinks._tables import TableSink, TableWrite, require_mode
from flux_engine.sinks.errors import FLX_3504
from flux_engine.sinks.receipts import StreamContext

if TYPE_CHECKING:  # pragma: no cover
    import pyarrow

deltalake = require("deltalake", extra="publish", feature="the delta sink")

__all__ = ["DEFAULT_MODE", "MODES", "OPTIONS", "DeltaSink", "DeltaTarget", "target"]

#: The options ``--sink-opt`` may set, as the command line spells them.
OPTIONS = ("instance-identity", "mode")

MODES = ("error", "append", "overwrite")
DEFAULT_MODE = "error"

#: ``scheme://`` at the start of a target.
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


class DeltaSink(TableSink):
    """Writes one stream as the Delta table ``<destination>/<exposeId>``.

    Parameters
    ----------
    destination
        An :class:`~flux_engine.sinks._objects.ObjectLocation` or a local directory.
    context
        The stream's run, expose, row count and ``outputDigest``.
    mode
        ``error`` (default), ``append`` or ``overwrite``.
    """

    name = "delta"

    def __init__(
        self,
        destination: Union[Path, ObjectLocation],
        context: StreamContext,
        *,
        mode: str = DEFAULT_MODE,
    ) -> None:
        super().__init__(context)
        self.mode = require_mode(self.name, mode, MODES)
        expose = require_segment(context.expose_id)
        self._table_path: Optional[Path] = None
        if isinstance(destination, ObjectLocation):
            self._raw_target = f"{destination.store_url}/{destination.prefix}"
            self._target = destination.url
            self._location = f"{destination.store_url}/{destination.key(expose)}"
        else:
            self._raw_target = str(destination)
            self._target = target_label(self.name, self._raw_target)
            self._table_path = destination / expose
            self._location = str(self._table_path)

    @property
    def target(self) -> str:
        return self._target

    @property
    def location(self) -> str:
        """The table's location as a label: no user information, query or fragment."""
        return target_label(self.name, self._location)

    def _known_strings(self) -> List[str]:
        return [self._location, self._raw_target]

    def _publish(self, table: "pyarrow.Table") -> TableWrite:
        if self._table_path is not None:
            # A table directory that is a link would send the write where the link points.
            require_real_directory(self._table_path)
        deltalake.write_deltalake(self._location, table, mode=self.mode)
        version = deltalake.DeltaTable(self._location).version()
        return TableWrite(
            target=self._target,
            table=self.location,
            writer={
                "library": "deltalake",
                "version": deltalake.__version__,
                "writer": f"deltalake {deltalake.__version__}",
                "mode": self.mode,
                "tableVersion": version,
            },
        )


_PROBE = StreamContext(
    run_id="probe", expose_id="probe", rows=0, output_digest=digest_of_bytes(b"", subject="probe")
)


@dataclass(frozen=True)
class DeltaTarget:
    """The Delta sink's checked settings, handing out one sink per stream."""

    target: str
    mode: str = DEFAULT_MODE
    name: str = "delta"
    #: ``--sink-opt instance-identity=true``: an object store may use the instance's own credentials.
    instance_identity: bool = False

    def __post_init__(self) -> None:
        # The target and the mode are checked now, before the simulation runs.
        self._sink(_PROBE)
        destination = self._destination()
        if isinstance(destination, ObjectLocation):
            # deltalake builds its own store from the environment, and would ask the cloud's
            # instance metadata service for credentials it did not find there.
            require_explicit_credentials(destination, library="deltalake")

    def _destination(self) -> Union[Path, ObjectLocation]:
        text = self.target
        if not text.strip():
            raise FluxError(
                FLX_3504, "the delta sink needs a target: --sink delta:DIRECTORY or delta:URL"
            )
        if is_object_target(text):
            return parse_object_target(text, instance_identity=self.instance_identity)
        if _SCHEME.match(text):
            raise FluxError(
                FLX_3504,
                f"{target_label(self.name, text)!r} is not a directory or an s3://, gs://, az:// or abfs:// URL",
            )
        # The target as typed, not resolved: a resolved link would no longer look like one.
        directory = require_real_directory(Path(text))
        return directory.resolve()

    def _sink(self, context: StreamContext) -> DeltaSink:
        return DeltaSink(self._destination(), context, mode=self.mode)

    def sink_for(self, context: StreamContext) -> DeltaSink:
        """A fresh sink for one stream."""
        return self._sink(context)


def target(location: str, options: Mapping[str, str]) -> DeltaTarget:
    """The ``delta`` sink's factory: ``--sink delta:DIRECTORY`` or ``delta:URL``, and ``mode``."""
    require_known("delta", options, OPTIONS)
    return DeltaTarget(
        target=location,
        mode=text_option(options, "mode", DEFAULT_MODE) or "",
        instance_identity=instance_identity_option(options),
    )
