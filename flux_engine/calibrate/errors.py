# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Error codes of fitting declared parameters to consumer query rates (ADR 0028 sections 8 and 9).

Standard library and the error catalog only, so ``fluxsim explain`` and the documentation
generator load them without cma, sqlglot, DuckDB or pyarrow installed.
"""

from __future__ import annotations

from flux_engine.core.errors import register

__all__ = [
    "FLX_3704",
    "FLX_3705",
    "FLX_3706",
    "FLX_3707",
    "FLX_3708",
    "FLX_3709",
    "FLX_3710",
    "FLX_3711",
    "FLX_3712",
    "FLX_7101",
]

FLX_3704 = register(
    "FLX-3704",
    "--max-evaluations is outside the range the engine allows",
    "A fit runs the bundle once for each candidate it tries, and the engine's ceiling is 64 "
    "evaluations. --max-evaluations raises that ceiling for the one command, up to 4,096, when "
    "the bundle's calibration block declares no evaluations of its own; a bundle's own value is "
    "kept, may only lower the ceiling, and is read against the engine's 64 in every stage "
    "(FLX-3616). A value below 64 or above 4,096 is refused, and the message names the range. "
    "Nothing was run.",
)

FLX_3705 = register(
    "FLX-3705",
    "a calibration parameter rests on a source a fit may not move",
    "A fit moves only what the data does not state. A parameter the calibration block names has "
    "to be a number the parameter register lists with the source assumption or default; a value "
    "the pinned profile or the contract states (the source profile or contract), or one an "
    "earlier fit already calibrated, is refused. The message names the document, the pointer and "
    "the source. Remove the parameter from calibration.parameters, or state the number as an "
    "assumption in the register if it is one. Nothing was run.",
)

FLX_3706 = register(
    "FLX-3706",
    "a calibration parameter names a document or a number the bundle does not have",
    "Each parameter of the calibration block names a document by its file name, one of the FLUX "
    "documents of the bundle's closed set (ADR 0006), and a JSON Pointer to one number the "
    "parameter register lists in it. A file outside that set, a file that holds more than one "
    "document, a pointer that reaches no number the register counts, a value outside the "
    "parameter's min and max, a step with no value between them, or two parameters that name "
    "the same number is refused, and the message says which. Nothing was run.",
)

FLX_3707 = register(
    "FLX-3707",
    "there is nothing to fit",
    "fluxsim calibrate --fit needs a bundle that declares consumers, a calibration block with "
    "at least one parameter, and a pinned profile whose queries section states the rate of at "
    "least one calibration query. The held-out queries are never fitted. The message names "
    "what is missing. Nothing was run.",
)

FLX_3708 = register(
    "FLX-3708",
    "fitting needs the calibrate extra",
    "The fit runs CMA-ES, which the [calibrate] extra installs together with the SQL libraries "
    "the queries need: pip install 'flux-sim-engine[calibrate]'. Declared calibration, "
    "fluxsim calibrate BUNDLE, needs none of it. Nothing was run.",
)

FLX_3709 = register(
    "FLX-3709",
    "the bundle's queries were checked with another sqlglot than the one installed",
    "fluxsim init records the sqlglot version that compiled the consumers' queries "
    "(compiledWith). Another version may write a query's canonical text another way, so a rate "
    "taken with it is not the rate the bundle's author checked, and the fit is not started. "
    "Install the recorded version, or write the queries again with fluxsim init. Nothing was "
    "run.",
)

FLX_3710 = register(
    "FLX-3710",
    "the place a fit writes is taken or is not usable",
    "fluxsim calibrate --fit --out DIR writes the calibration patch as DIR.patch.json and, when "
    "the held-out error holds, the calibrated bundle at DIR. Neither may exist yet, DIR may not "
    "be the input bundle or inside it, and its parent directory has to exist. This is checked "
    "before the fit starts, so a long fit is never lost to a name that was taken. Choose another "
    "--out. Nothing was run.",
)

FLX_3711 = register(
    "FLX-3711",
    "a calibration patch operation cannot be applied to its document",
    "A calibration patch holds RFC 6902 replace operations on declared numbers, and a replace "
    "needs its target to exist, so it adds nothing. The operation is applied to the text of the "
    "document, which keeps every other byte as it was, and that is possible only for a number "
    "written as a single plain or quoted scalar in a document that uses no alias or merge key. A "
    "pointer that reaches no member, a value that is a mapping, a list or a block scalar, and a "
    "document that aliases values are refused, and the message names the document and the "
    "pointer. Nothing was written.",
)

FLX_3712 = register(
    "FLX-3712",
    "the held-out error does not hold, so the bundle was not called calibrated",
    "A calibration is calibrated only when at least one held-out query has a target and every "
    "held-out query that has one falls within its tolerance (its own tolerance, or else the "
    "calibration block's heldOutTolerance). Otherwise fluxsim calibrate --fit writes the "
    "calibration patch and reports each held-out query that missed, with its gap and why, exits "
    "with status 1, and writes no bundle at --out. A held-out query that could not be counted, "
    "or that has no tolerance to be held to, is a miss. The held-out queries are never fitted: "
    "to meet them, change what the fit may move or ask the profiler for a better pinned "
    "profile, and run the fit again.",
)

FLX_7101 = register(
    "FLX-7101",
    "a candidate of the fit could not be run",
    "Each evaluation of a fit applies the candidate values to a copy of the bundle, plans and "
    "applies it at the calibration's scale, and counts the calibration queries on the run. When "
    "that run was refused (a candidate outside what the bundle accepts, or a run that failed) "
    "the fit stops: the message names the evaluation and the refusal. Nothing was written.",
)
