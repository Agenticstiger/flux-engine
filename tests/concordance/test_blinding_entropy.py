# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A blinding value must be drawn, not declared.

SEAM-FIDELITY-SPEC §4 asks for "a per-state blinding value that never
leaves the boundary", and ``boundary/slice.py`` states the intent: "a
guessable blinding value is no blinding value". The only guard was a
LENGTH check, which ``bytes(32)`` -- a public constant -- satisfies, and
both production call sites (``stages/ship.py`` and ``stages/verify.py``)
passed exactly that, making ``C2 = sha256(canonical S || 0^32)``
recomputable by anyone who can guess ``S``.
"""

from __future__ import annotations

import pytest

from flux_engine.concordance.boundary.slice import (
    MINIMUM_BLINDING_BYTES,
    generate_blinding,
    refuse_weak_blinding,
)
from flux_engine.core.errors import FluxError

pytestmark = pytest.mark.unit


def test_a_short_blinding_value_is_refused() -> None:
    with pytest.raises(FluxError):
        refuse_weak_blinding(b"\x01" * (MINIMUM_BLINDING_BYTES - 1))


@pytest.mark.parametrize("constant", [bytes(32), b"\xff" * 32, b"\x2a" * 64], ids=repr)
def test_a_constant_blinding_value_is_refused(constant: bytes) -> None:
    """The negative control: the exact value both call sites used to pass."""
    with pytest.raises(FluxError) as raised:
        refuse_weak_blinding(constant)
    assert "public constant" in str(raised.value)


def test_a_generated_blinding_value_is_accepted_and_never_repeats() -> None:
    first = generate_blinding()
    refuse_weak_blinding(first)
    assert len(first) == MINIMUM_BLINDING_BYTES
    assert first != generate_blinding()


def test_no_production_call_site_passes_a_constant() -> None:
    """Grep-as-a-test: the two producer sites must not name a literal again."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "flux_engine"
    offenders = [
        str(path.relative_to(root))
        for path in sorted(root.rglob("*.py"))
        if "_vendor" not in path.parts and "blinding=bytes(" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], offenders
