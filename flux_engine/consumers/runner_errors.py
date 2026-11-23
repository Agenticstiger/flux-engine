# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Error codes of the query process (ADR 0028 section 3).

Standard library and the error catalog only, so ``fluxsim explain`` and the documentation
generator load them without DuckDB or pyarrow installed.
"""

from __future__ import annotations

from flux_engine.core.errors import register

__all__ = ["FLX_7131", "FLX_7132", "FLX_7133", "FLX_7134", "FLX_7135"]

FLX_7131 = register(
    "FLX-7131",
    "consumer queries need an optional library that is not installed",
    "Consumer queries run in DuckDB and read the run's tables as Arrow, so they need duckdb and "
    "pyarrow, which the sql extra installs: pip install 'flux-sim-engine[sql]'. Generation "
    "reads no SQL, so nothing else in the engine needs either library. Install the extra and run "
    "the command again; nothing was counted.",
)

FLX_7132 = register(
    "FLX-7132",
    "a message to the query process passes its bound",
    "The engine sends the query process each table as Arrow IPC bytes and each count as a small "
    "JSON header, and bounds both: a header may hold 4 MiB and a table 1 GiB. The message names "
    "what passed its bound. Nothing was sent and no query ran. Use a smaller --scale for the run "
    "the queries read, or shorten the query text.",
)

FLX_7133 = register(
    "FLX-7133",
    "the query process was ended: the queries passed their time",
    "The queries of a run may take their seconds each (limits.seconds, 30 at most unless "
    "--max-query-seconds raises it), and the whole batch may take the sum of those plus 30 seconds. "
    "A query that passes its own seconds is interrupted and takes the status timedOut. A batch "
    "that passes the sum is a process that has stopped answering, so the engine ends it, first "
    "with a request to stop and then by force, and counts nothing. Run the command again; if it "
    "repeats, lower the work the queries do or raise --max-query-seconds.",
)

FLX_7134 = register(
    "FLX-7134",
    "the query process did not start as asked",
    "The engine starts one process for the counts, with an environment of its own, and reads "
    "its first message before it sends a table: the versions, the limits it set and the "
    "keys of its environment. The process ended or answered something else first, or refused a "
    "table. The message says which. Run the command again; if it repeats, check that the "
    "python environment that runs fluxsim can import duckdb and pyarrow with no setting of the "
    "shell.",
)

FLX_7135 = register(
    "FLX-7135",
    "a run's emitted stream cannot be read as a table",
    "The queries read the files under the run's emits/ directory, and only those, without "
    "following a link. A stream is one row per line, each a JSON array of as many values as the "
    "contract's columns, and no float. The message names the stream and the line; it never "
    "quotes a value. Run apply again, and do not edit a file under emits/.",
)
