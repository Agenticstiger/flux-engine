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

from typing import Any

__all__ = ["__version__"]


def __getattr__(name: str) -> Any:
    # Read on first use: `importlib.metadata` costs about 30 ms to import and a worker
    # process, or any command that never prints the version, has no use for it.
    if name == "__version__":
        from importlib import metadata

        # The local keeps the name __version__: scripts/release_guard.py reads the fallback
        # literal from an assignment to that name in an except branch.
        try:
            __version__ = metadata.version("flux-sim-engine")
        except metadata.PackageNotFoundError:  # pragma: no cover - editable/dev tree
            __version__ = "0.13.1"
        globals()["__version__"] = __version__
        return __version__
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
