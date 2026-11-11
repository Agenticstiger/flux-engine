# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Re-export of the FLUX offline reference validator (``flux_spec.validate``).

Tries the public ``flux_spec`` distribution first; falls back to the
vendored copy at ``flux_engine._vendor.flux_spec.validate``.
"""

from __future__ import annotations

try:
    from flux_spec.validate import (
        Bundle,
        canonical_digest,
        load_yaml_docs,
        semver_satisfies,
    )
except ImportError:
    from flux_engine._vendor.flux_spec.validate import (
        Bundle,
        canonical_digest,
        load_yaml_docs,
        semver_satisfies,
    )

__all__ = ["Bundle", "canonical_digest", "load_yaml_docs", "semver_satisfies"]
