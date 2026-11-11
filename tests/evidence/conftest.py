# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for the evidence-pack suite.

Mirrors ``tests/expr/conftest.py``'s own resolution of the FLUX spec
repo's shipped telco bundle -- the one real, schema-valid FLUX bundle this
test tree has on hand, used here for the tests that need one (packing
requires an actual, validating bundle; a hand-built fixture would either
duplicate the whole FLUX schema or not exercise the real path).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]

_EXAMPLE_CANDIDATES = (
    REPO_ROOT.parent / "FLUX" / "examples" / "telco-payment-recovery",
    REPO_ROOT / "examples" / "telco-payment-recovery",
)


def telco_example_dir() -> Optional[Path]:
    """Return the shipped telco bundle directory, or ``None`` if not present."""
    for candidate in _EXAMPLE_CANDIDATES:
        if (candidate / "world.flux.yml").is_file():
            return candidate
    return None
