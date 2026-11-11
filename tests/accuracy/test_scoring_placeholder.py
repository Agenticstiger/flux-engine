# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""flux_engine.accuracy._scoring is empty in v0.1 -- mechanism lands in v0.3.

Mirrors tests/test_placeholders.py's convention: a placeholder's current
shape (``__all__ == []``) is a tracked contract, not unexercised code
coverage would otherwise flag as dead.
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_scoring_placeholder_exports_nothing_yet() -> None:
    import flux_engine.accuracy._scoring as scoring_module

    assert scoring_module.__all__ == []
