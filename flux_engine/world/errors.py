# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Error codes raised while materialising a world (the ``FLX-5040`` block).

KERNEL-SPEC §13 gives band 5000 to numerics. Population materialisation is
where a declared distribution becomes a drawn value, so a declaration this
engine cannot sample -- an unknown ``dist``, a weight that is not a number, a
normal whose bounds cross -- fails here with a 5000-band code rather than
travelling into a digest as a silently substituted default.

Registered at import time so ``fluxsim explain FLX-5040`` works without a
population having been materialised first (see
:func:`flux_engine.core.errors.ensure_core_codes_loaded`).
"""

from __future__ import annotations

from flux_engine.core.errors import ErrorCode, register

__all__ = ["FLX_5040", "FLX_5041"]

FLX_5040: ErrorCode = register(
    "FLX-5040",
    "population declaration cannot be materialised",
    "A World or Persona declaration reached the sampler in a shape it cannot draw from: "
    "an unknown trait 'dist', a non-numeric weight or bound, a normal whose 'min' exceeds "
    "its 'max', or a market list the assignment cannot index. Fix the declaration; the "
    "engine will not substitute a default, because a substituted default would change the "
    "population digest without changing the documents.",
)

FLX_5041: ErrorCode = register(
    "FLX-5041",
    "value is outside the domain the kernel can hash",
    "A schema-declared JSON number could not be converted to the exact Decimal the kernel "
    "hashes (KERNEL-SPEC §2.1 keeps float out of every hashed value), or a probability "
    "landed outside [0, 1]. The offending value's class and path are named; the value "
    "itself is not, so a diagnostic never leaks a population value.",
)
