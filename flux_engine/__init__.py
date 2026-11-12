# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""FLUX Simulation Engine -- the runtime for FLUX bundles.

The sibling of the FLUID standard and CLI (``fluid``, ``data-product-forge``):
FLUX is the "wind tunnel" that runs a declarative simulation bundle and emits,
under a referenced FLUID contract, the exact rows that contract will see in
production.
"""

from __future__ import annotations

from importlib import metadata as _metadata

try:
    __version__ = _metadata.version("flux-sim-engine")
except _metadata.PackageNotFoundError:  # pragma: no cover - editable/dev tree, no metadata yet
    __version__ = "0.3.1"

__all__ = ["__version__"]
