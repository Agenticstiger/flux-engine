# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The outcome-accuracy scoring mechanism -- mechanism lands in v0.3.

Nothing here today: :mod:`flux_engine.accuracy.records` answers v0.1's one
question (did the observed value land inside the declared interval?) by
plain containment, which is not a proper scoring rule. A proper scoring
rule -- and the numeric grade it would let the engine compute rather than
only declare -- is out of scope until v0.3.

Import-linter fences this package off in the meantime (see the
"accuracy scoring mechanism is not wired until v0.3" contract requested
against ``pyproject.toml`` in this package's build report): nothing in
``flux_engine`` may import ``flux_engine.accuracy._scoring`` while it is
empty, so an early, accidental dependency on a mechanism that does not
exist yet is a lint failure, not a runtime surprise the day v0.3 changes
what importing it does.
"""

from __future__ import annotations

__all__ = []
