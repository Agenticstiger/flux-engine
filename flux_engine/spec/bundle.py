# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Re-export of the FLUX evidence bundler (``flux_spec.bundle``).

``MANIFEST_NAME`` is re-exported beside the three functions because the
engine has to name the file the bundler writes, and naming it with a
string literal of its own would be a hand-mirror of a constant that lives
upstream. ``MAX_MEMBERS`` and ``MAX_BUNDLE_BYTES`` are the caps ``verify``
holds an archive to before it extracts anything, which the engine's own read
of an archive stays inside. :mod:`flux_engine.seam.resolve` builds the set of pack products
from this name, so a rename in the bundler this engine links against
carries here without an edit.
"""

from __future__ import annotations

from flux_spec.bundle import MANIFEST_NAME, MAX_BUNDLE_BYTES, MAX_MEMBERS, merkle_root, pack, verify

__all__ = ["MANIFEST_NAME", "MAX_BUNDLE_BYTES", "MAX_MEMBERS", "pack", "verify", "merkle_root"]
