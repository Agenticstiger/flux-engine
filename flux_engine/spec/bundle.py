# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Re-export of the FLUX evidence bundler (``flux_spec.bundle``).

Tries the public ``flux_spec`` distribution first; falls back to the
vendored copy at ``flux_engine._vendor.flux_spec.bundle``.

``MANIFEST_NAME`` is re-exported beside the three functions because the
engine has to name the file the bundler writes, and naming it with a
string literal of its own would be a hand-mirror of a constant that lives
upstream. :mod:`flux_engine.seam.resolve` builds the set of pack products
from this name, so a rename in the bundler this engine links against
carries here without an edit.
"""

from __future__ import annotations

try:
    from flux_spec.bundle import MANIFEST_NAME, merkle_root, pack, verify
except ImportError:
    from flux_engine._vendor.flux_spec.bundle import MANIFEST_NAME, merkle_root, pack, verify

__all__ = ["MANIFEST_NAME", "pack", "verify", "merkle_root"]
