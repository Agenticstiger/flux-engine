# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Error codes of the consumer queries ``verify`` runs (ADR 0028 sections 3, 4 and 7).

Standard library and the error catalog only, so ``fluxsim explain`` and the documentation
generator load them without sqlglot, DuckDB or pyarrow installed.
"""

from __future__ import annotations

from flux_engine.core.errors import register

__all__ = ["FLX_3700", "FLX_3701", "FLX_3702", "FLX_3703"]

FLX_3700 = register(
    "FLX-3700",
    "a query ceiling flag is outside the range the engine allows",
    "--max-query-seconds raises the wall clock a consumer query may take from the engine's 30 "
    "seconds to at most 600, and --max-query-memory raises DuckDB's memory limit from 1GiB to at "
    "most 16GiB. A value below the engine's own ceiling or above the largest is refused, since a "
    "flag only raises the runtime ceiling and never lowers it (a bundle lowers its own limits, "
    "and its own value is kept). The message "
    "names the flag and its range. Nothing was run.",
)

FLX_3701 = register(
    "FLX-3701",
    "the pinned profile holds the rate of a held-out query",
    "A held-out query is one the calibration never saw, so its target comes from the separate "
    "held-out profile (--held-out) and never from the profile the bundle pins. A pinned profile "
    "whose queries section names a held-out query would let the calibration fit what it is to be "
    "judged on. The message names the query. Ask the profiler for the pinned profile without that "
    "entry, or declare the query as a calibration query. Nothing was run.",
)

FLX_3702 = register(
    "FLX-3702",
    "the held-out profile is not apart from the calibration queries",
    "The profile given to --held-out answers the held-out queries only. A profile that names a "
    "calibration query is refused, so that the two sets of targets stay apart. A bundle that "
    "declares no held-out query has nothing to read the file for, and `verify` refuses it too; "
    "`calibrate --fit` instead fits such a bundle, does not read the file and says no held-out "
    "check was made. The message names the query, or the missing declaration. Nothing was run.",
)

FLX_3703 = register(
    "FLX-3703",
    "the run's emitted stream does not digest to what the receipt recorded",
    "Before the consumers' queries read a run's rows, each emitted stream is digested as the "
    "run's outputDigest is, and compared with the receipt's, so that a count is always taken over "
    "the rows the run produced. The message names the stream. Run apply again, and do not edit a "
    "file under emits/.",
)
