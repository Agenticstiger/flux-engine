# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Write a FLUX bundle from a FLUID contract and a profile, for ``fluxsim init --profile``.

:func:`author_bundle` reads the two documents, and no record, and returns the
files of a bundle that validates: the contract, a World, a Persona and a
Simulation whose generator emits are declared from the profile's published
aggregates, with every number labelled in the parameter register.
"""

from __future__ import annotations

from flux_engine.author.bundle import (
    FLUX_VERSION,
    AuthoredBundle,
    AuthoredExpose,
    AuthorError,
    AuthorInputs,
    Gap,
    StrictFindings,
    author_bundle,
    strict_findings,
)

__all__ = [
    "FLUX_VERSION",
    "AuthorError",
    "AuthoredBundle",
    "AuthoredExpose",
    "AuthorInputs",
    "Gap",
    "StrictFindings",
    "author_bundle",
    "strict_findings",
]
