# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A draw whose value cannot be held at its declared scale is refused, as ``FLX-5021``.

``core.rng`` quantises every continuous draw, and the bounds of a truncated
normal, onto the declared scale in a 50-digit working context. A value that
needs more digits than that -- a bound of 1e44 at six places, an
exponential wait of 1e60 at six places -- made ``Decimal.quantize`` raise a
bare ``decimal.InvalidOperation``. ``FLX-5021`` already names this case ("a
scale on which no value of the declared interval is representable") and the
kernel's rule is to refuse with a typed error rather than crash, so a
schema-valid Persona trait such as ``{dist: normal, stddev: 1.25e43}`` now
fails with that code.

Found by the accuracy program's schema-driven property tests
(docs/accuracy/DESIGN.md).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.rng import DrawStream, exponential, truncated_normal
from flux_engine.world.population import PersonaSpec

pytestmark = pytest.mark.unit


def _stream() -> DrawStream:
    return DrawStream.from_entropy(20261111)


def test_bounds_beyond_the_working_precision_are_refused_with_flx_5021() -> None:
    with pytest.raises(FluxError) as refused:
        truncated_normal(
            _stream(),
            mean=Decimal(0),
            stddev=Decimal("1.25e43"),
            lo=Decimal("-1e44"),
            hi=Decimal("1e44"),
            scale=6,
        )
    assert refused.value.code.code == "FLX-5021"


def test_a_wait_beyond_the_working_precision_is_refused_with_flx_5021() -> None:
    with pytest.raises(FluxError) as refused:
        exponential(_stream(), rate=Decimal("1e-60"), scale=6)
    assert refused.value.code.code == "FLX-5021"


def test_a_schema_valid_persona_trait_is_refused_not_crashed() -> None:
    persona = {
        "id": "p",
        "spec": {"traits": {"x": {"dist": "normal", "mean": 0.0, "stddev": 1.25e43}}},
    }
    family = PersonaSpec.from_document(persona).traits["x"]
    with pytest.raises(FluxError) as refused:
        family.draw(_stream)
    assert refused.value.code.code == "FLX-5021"


def test_representable_draws_are_unchanged() -> None:
    value = truncated_normal(
        _stream(), mean=Decimal(50), stddev=Decimal(10), lo=Decimal(0), hi=Decimal(100), scale=6
    )
    assert Decimal(0) <= value <= Decimal(100)
    assert value == value.quantize(Decimal("0.000001"))
