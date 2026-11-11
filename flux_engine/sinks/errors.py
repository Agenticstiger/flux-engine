# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Error codes raised by the output sinks.

KERNEL-SPEC §13 gives 3000 to "seam/conformance", and a sink is the last
thing in the emit seam, so its codes sit in that band, in the ``FLX-35nn``
block after the generator's ``FLX-34nn``.

This module imports nothing beyond the error catalog, so ``fluxsim
explain`` and the documentation generator can load every sink code without
any optional extra installed.
"""

from __future__ import annotations

from flux_engine.core.errors import ErrorCode, register

__all__ = ["FLX_3501", "FLX_3502", "FLX_3503", "FLX_3504", "FLX_3505"]

FLX_3501: ErrorCode = register(
    "FLX-3501",
    "a sink needs an optional extra that is not installed",
    "The engine's core installs without any columnar or database library, so the sinks "
    "that need one live behind optional extras: the Arrow, Parquet and Arrow IPC sinks "
    "need pyarrow, from flux-sim-engine[arrow], and the database sink needs "
    "adbc-driver-manager, from flux-sim-engine[adbc], plus the ADBC driver for the "
    "database. Install the extra the message names, for example "
    "pip install 'flux-sim-engine[arrow]', and run the command again. Nothing was "
    "written: the check runs before a sink opens.",
)

FLX_3502: ErrorCode = register(
    "FLX-3502",
    "a value has no exact representation in the sink's format",
    "A sink writes every value exactly or not at all. An INT outside the signed 64-bit "
    "range has no Arrow integer type to hold it, and a DECIMAL whose precision exceeds "
    "76 digits has no Arrow decimal type, so the sink refuses the stream rather than "
    "round, wrap or truncate a value the run computed. The message names the column, "
    "the row index and the value's class, never the value. Narrow the column's declared "
    "type in the FLUID contract, or write this stream with a sink that keeps values as "
    "text, such as the run directory's own NDJSON file.",
)

FLX_3503: ErrorCode = register(
    "FLX-3503",
    "the data read back is not the stream it is read as",
    "Reading rows back from a file or a table needs to know which stream they belong to: "
    "each column's logical type and the stream's header, which the sinks record in the "
    "Arrow metadata they write. Data without that metadata, with columns renamed or "
    "re-ordered, or with a column stored in a type that cannot hold its declared logical "
    "type (a BOOL kept as text, a DECIMAL at another scale) cannot be turned back into "
    "the rendered rows the outputDigest covers. Pass the header and the logical types "
    "explicitly, or read the data the sink wrote rather than a copy that changed it.",
)

FLX_3504: ErrorCode = register(
    "FLX-3504",
    "a sink was given a target or an option it cannot use",
    "A sink writes only where it is told to and only in ways it can name, so it checks "
    "its target and its options before it opens: a file sink needs a directory that is "
    "not a file, a stream whose expose id is a safe single path segment, and options "
    "of the right type and range; every sink needs to be opened on the stream it was "
    "set up for. The message names the target or the option and says what it expects. "
    "Nothing was written.",
)

FLX_3505: ErrorCode = register(
    "FLX-3505",
    "a sink could not finish writing a stream",
    "The destination refused a write part-way: a disk filled up, a permission was "
    "missing, a file could not be renamed into place, or a read-back check found data "
    "that does not digest to the stream's outputDigest. A file sink stages every file "
    "under a hidden name and renames it into place only when the whole stream is "
    "written, and it removes what it staged when it fails, so no partial stream is left "
    "for a reader to mistake for a whole one. The run directory and its outputDigest are "
    "unaffected. Fix the cause the message names and run the command again: the same "
    "run writes the same files under the same names.",
)
