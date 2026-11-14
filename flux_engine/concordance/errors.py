# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The ``FLX-nnnn`` codes the fidelity check owns.

Registered here, at import time, into the one catalog
(``flux_engine.core.errors``) so ``fluxsim explain FLX-5010`` answers for
this subsystem the same way it answers for the kernel's. KERNEL-SPEC §13
fixes the bands; three of the codes below are named directly by
SEAM-FIDELITY-SPEC (``FLX-5010`` §4, ``FLX-5011`` §8, ``FLX-8001`` §7) and
the rest are this module's allocation inside those bands.

**Which band a refusal belongs in.** SEAM-FIDELITY-SPEC §2 says the block
is checked "by ``fluxsim validate`` (offline)", which might argue for the
3000 (seam/conformance) band. Every code the specification names for this
subsystem is in the 5000 (numerics) band instead -- ``FLX-5001`` and
``FLX-5002`` at compile time, ``FLX-5010`` and ``FLX-5011`` at check time
-- and every refusal below is a refusal to *form a statistic*, not a
refusal to conform a row. They are allocated in the 5000 band for that
reason, with the single exception of the two evidence-pack codes, which
sit in the 8000 band because they are about the pack, not the numbers.

Nothing here reads a clock, draws randomness, or holds state a run
depends on.
"""

from __future__ import annotations

from flux_engine.core.errors import ErrorCode, register

__all__ = [
    "FLX_5010",
    "FLX_5011",
    "FLX_5012",
    "FLX_5013",
    "FLX_5030",
    "FLX_5031",
    "FLX_5032",
    "FLX_5033",
    "FLX_5034",
    "FLX_5035",
    "FLX_8001",
    "FLX_8002",
    "OWNED_CODES",
]

FLX_5010: ErrorCode = register(
    "FLX-5010",
    "the check budget for this committed state is exhausted",
    "SEAM-FIDELITY-SPEC §4: the boundary counts the checks it answers for "
    "one committed state and refuses beyond budget.maxChecksPerState. The "
    "counter lives inside the boundary because a caller able to reset it "
    "could ask an unbounded number of questions of one fixed record set. "
    "Commit a new state, or raise the declared budget.",
)

FLX_5011: ErrorCode = register(
    "FLX-5011",
    "the committed statistic was formed under different declarations",
    "SEAM-FIDELITY-SPEC §8: a change to anything other than a tolerance, a "
    "minVariance or budget.maxStateAge moves G, and a statistic committed "
    "under the old G no longer answers for the new declarations. A new "
    "committed state is required before the fidelity check can run again.",
)

FLX_5012: ErrorCode = register(
    "FLX-5012",
    "the committed state is older than the declared maximum age",
    "SEAM-FIDELITY-SPEC §7: the ship gate compares the state's commitment "
    "time against budget.maxStateAge. A state older than that is refused "
    "rather than reused, so a fidelity record cannot be carried forward "
    "past the window its own declaration granted it.",
)

FLX_5013: ErrorCode = register(
    "FLX-5013",
    "the boundary store cannot supply this committed state's blinding value",
    "SEAM-FIDELITY-SPEC §4: C2 commits to the derived statistic under a "
    "per-state blinding value that never leaves the boundary. The value is "
    "drawn once, when the state is formed, and kept in the boundary store "
    "under the state directory, so every later check of that same state "
    "recomputes the same C2. This refusal means the store could not hand it "
    "back -- the store is unreadable or unwritable, or the entry for the "
    "state carries no blinding value. It is fail-closed on purpose: drawing "
    "a fresh value would publish a second, different C2 for a record set "
    "that has not changed. Restore the boundary store, or fix its "
    "permissions, and run the check again.",
)

FLX_5030: ErrorCode = register(
    "FLX-5030",
    "the fidelity block is malformed",
    "The block's shape does not match SEAM-FIDELITY-SPEC §2: a required "
    "section is missing, a section is not a mapping, a value has the wrong "
    "class, a number needs more than 1000 digits written out, or a closed "
    "set (a property name, a tolerance type, a null policy, a failure "
    "action) was given a member it does not have. The block is refused "
    "before any statistic is formed.",
)

FLX_5031: ErrorCode = register(
    "FLX-5031",
    "a column named by the fidelity block is not a member of the expose",
    "SEAM-FIDELITY-SPEC §2: every column and every groupBy is a member of "
    "the referenced expose schema. A column the expose does not declare "
    "has no type, no position in the emitted row, and no committed "
    "counterpart, so the block is refused offline rather than failing at "
    "check time.",
)

FLX_5032: ErrorCode = register(
    "FLX-5032",
    "histogram bins do not partition the declared range",
    "SEAM-FIDELITY-SPEC §2: bins are strictly increasing and partition the "
    "declared range. The first edge is the column's declared minimum and "
    "the last its declared maximum; otherwise a record could fall outside "
    "every bin and the two sides' bin counts would not account for the "
    "same records.",
)

FLX_5033: ErrorCode = register(
    "FLX-5033",
    "a correlation property is incompletely declared",
    "SEAM-FIDELITY-SPEC §2: a correlation property declares two columns "
    "and a minVariance. Without the second column there is nothing to "
    "correlate; without the variance floor the encoding error on a "
    "correlation has no bound (KERNEL-SPEC §12), so no margin can be "
    "compiled.",
)

FLX_5034: ErrorCode = register(
    "FLX-5034",
    "a parameter the declared property needs is absent",
    "SEAM-FIDELITY-SPEC §2: every property's parameters are present, and a "
    "contract missing any of them is refused before any statistic is "
    "formed. A histogram needs bins, a quantile needs a level, every "
    "property needs a tolerance of the type its comparison uses, and every "
    "column a property names needs its own declaration in columns.",
)

FLX_5035: ErrorCode = register(
    "FLX-5035",
    "the committed slice cannot be read as a committed state",
    "SEAM-FIDELITY-SPEC §4: the owner fixes the record set as a file:// "
    "slice. The slice was unreadable, carried a scheme other than file://, "
    "held a line that is not one JSON object, carried a binary "
    "floating-point number, or held more records than records.max leaves "
    "room to commit.",
)

FLX_8001: ErrorCode = register(
    "FLX-8001",
    "the ship gate withheld the bundle",
    "SEAM-FIDELITY-SPEC §7: on any failure the gate writes no bundle and "
    "emits com.agentics.flux.ship.withheld. The decision names every check "
    "that failed -- the recomputed H, the record's binding and signature, "
    "the recomputed output digest and statistics, the re-evaluated "
    "properties, the state's age -- so the reason is in the event, not in "
    "the exit code.",
)

FLX_8002: ErrorCode = register(
    "FLX-8002",
    "the fidelity record is malformed or its signature does not verify",
    "SEAM-FIDELITY-SPEC §6 fixes the record's shape and signs it with the "
    "verifier key. A record whose fields are missing or mistyped, whose "
    "scope statement is not the generated one, or whose signature does not "
    "verify against the named key, is refused: an unreadable record is not "
    "a weaker record, it is no record.",
)

#: Every code this subsystem registers, in sorted order. A test walks it
#: against the catalog so a code added here without an explanation, or an
#: explanation that drifts into the register-discipline vocabulary, is a
#: failure rather than a surprise in ``fluxsim explain``.
OWNED_CODES: tuple[ErrorCode, ...] = (
    FLX_5010,
    FLX_5011,
    FLX_5012,
    FLX_5013,
    FLX_5030,
    FLX_5031,
    FLX_5032,
    FLX_5033,
    FLX_5034,
    FLX_5035,
    FLX_8001,
    FLX_8002,
)
