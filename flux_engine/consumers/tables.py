# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The tables the queries read: a run's emitted files, as Arrow tables with the contract's types.

The queries read the run's ``emits/<exposeId>.ndjson`` files and nothing else of the run: not the
population, not the decisions, not a profile. Each file is read through :mod:`flux_engine.safeio`, so a
name that is a link, a directory or a device is refused and nothing is followed, and the directory
itself must not be a link. A stream is one row per line, each a JSON array of as many values as the
header has columns; the rows are batched into Arrow by :class:`flux_engine.sinks.arrow.BatchBuilder`, so a
column has the type its contract declares (an ``INT`` its width, a ``DECIMAL`` its precision and scale)
and a value that does not fit is refused as the sinks refuse it (``FLX-3502``).

The file's digest is not recomputed here: the caller that has the run's receipt does that, as
:func:`flux_engine.stages.deliver.deliver_run` does before it hands rows to a sink.

pyarrow is imported when :func:`tables_from_run` is called, never when this module is.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Sequence, Tuple, Union

from flux_engine.api.emit import EmitHeader
from flux_engine.consumers.runner import ArrowTable
from flux_engine.consumers.runner_errors import FLX_7131, FLX_7135
from flux_engine.core.errors import FluxError
from flux_engine.safeio import json_lines, read_bytes_nofollow, require_real_directory
from flux_engine.sinks._optional import is_available

__all__ = ["RunStream", "tables_from_run"]


@dataclass(frozen=True)
class RunStream:
    """One expose of the run: its header, and the contract's own spellings of the column types.

    ``declared_types`` are aligned to the header's columns (``smallint``, ``decimal(18, 4)``), as
    :func:`flux_engine.stages.deliver.deliver_run` passes them to a sink; empty keeps every integer
    an ``int64``.
    """

    header: EmitHeader
    declared_types: Tuple[str, ...] = ()


def _refuse_number(text: str) -> Any:
    raise ValueError("a float or a non-finite number")


def _rows(expose_id: str, data: bytes, width: int) -> Iterator[List[Any]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FluxError(FLX_7135, f"the stream {expose_id!r} is not UTF-8") from error
    for number, line in enumerate(json_lines(text)):
        try:
            row = json.loads(line, parse_float=_refuse_number, parse_constant=_refuse_number)
        except (ValueError, RecursionError) as error:
            raise FluxError(
                FLX_7135,
                f"the stream {expose_id!r}, line {number}, is not a JSON array of exact values",
            ) from error
        if not isinstance(row, list) or len(row) != width:
            raise FluxError(
                FLX_7135,
                f"the stream {expose_id!r}, line {number}, is not an array of the {width} declared columns",
            )
        yield row


def _name_of(expose_id: str) -> str:
    if (
        not expose_id
        or expose_id in (".", "..")
        or any(ch in expose_id for ch in ("/", "\\", "\0"))
    ):
        raise FluxError(FLX_7135, f"the exposeId {expose_id!r} is not a single file name")
    return f"{expose_id}.ndjson"


def tables_from_run(
    emits_dir: Union[str, Path], streams: Sequence[RunStream]
) -> Dict[str, ArrowTable]:
    """Each stream of *streams* as an Arrow table, read from ``emits_dir/<exposeId>.ndjson``.

    ``FLX-2118`` for a *emits_dir* that is a link, ``FLX-2117`` for a stream file that is a link or
    not a regular file, ``FLX-7135`` for a file that is missing or holds a line that is not a row of
    its header, ``FLX-7131`` when pyarrow is not installed.
    """
    if not is_available("pyarrow"):
        raise FluxError(
            FLX_7131,
            "consumer queries need pyarrow, which is not installed: pip install 'flux-sim-engine[sql]'",
        )
    from flux_engine.sinks.arrow import BatchBuilder, pa

    directory = require_real_directory(emits_dir)
    tables: Dict[str, ArrowTable] = {}
    for stream in streams:
        header = stream.header
        name = _name_of(header.expose_id)
        try:
            data = read_bytes_nofollow(directory / name)
        except FluxError:
            raise  # a link or a special file: FLX-2117, as the engine reports it everywhere
        except OSError as error:
            raise FluxError(
                FLX_7135, f"the stream {header.expose_id!r} cannot be read: {type(error).__name__}"
            ) from error
        builder = BatchBuilder(header, declared_types=stream.declared_types)
        batches = list(builder.extend(_rows(header.expose_id, data, len(header.columns))))
        if not batches:
            batches = [builder.empty_batch()]
        tables[header.expose_id] = _naive_timestamps(
            pa.Table.from_batches(batches, schema=builder.schema), pa
        )
    return tables


def _naive_timestamps(table: Any, pa: Any) -> Any:
    """*table* with each UTC timestamp column as a timestamp with no zone, holding the same instants.

    The checker types a TIMESTAMP column as DuckDB's ``TIMESTAMP`` (ADR 0028 section 2), so the table
    carries that type too, and a comparison with a timestamp literal involves no time zone at all.
    """
    fields = []
    changed = False
    for field in table.schema:
        if pa.types.is_timestamp(field.type) and field.type.tz is not None:
            fields.append(pa.field(field.name, pa.timestamp(field.type.unit), field.nullable))
            changed = True
        else:
            fields.append(field)
    return table.cast(pa.schema(fields)) if changed else table
