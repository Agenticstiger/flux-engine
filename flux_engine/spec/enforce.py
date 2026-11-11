# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Re-export of the FLUX RFC-04 enforcement gate (``flux_spec.enforce``).

Tries the public ``flux_spec`` distribution first; falls back to the
vendored copy at ``flux_engine._vendor.flux_spec.enforce``.

Only ``flux_engine.governance.gate`` may import this module -- see the
``[tool.importlinter]`` contract "only governance.gate may import
spec.enforce" in pyproject.toml. Every other AI-call or conformance-check
path goes through ``flux_engine.governance.gate`` instead of here.
"""

from __future__ import annotations

try:
    from flux_spec.enforce import (
        DEFAULT_VECTORS,
        decide,
        run_vectors,
    )
except ImportError:
    from flux_engine._vendor.flux_spec.enforce import (
        DEFAULT_VECTORS,
        decide,
        run_vectors,
    )

__all__ = ["decide", "run_vectors", "DEFAULT_VECTORS"]
