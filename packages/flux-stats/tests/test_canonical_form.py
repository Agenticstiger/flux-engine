# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The canonical form a FLUX digest is taken over (KERNEL-SPEC §2.1)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from flux_stats.canonical import JCS_MAX_SAFE_INTEGER, canonicalise, coerce_key, render_decimal
from flux_stats.errors import FluxError

pytestmark = pytest.mark.unit

#: An e with an acute accent, composed (NFC) and decomposed (NFD).
COMPOSED = chr(0x00E9)
DECOMPOSED = "e" + chr(0x0301)


def test_keys_are_coerced_strings_are_nfc_and_mappings_are_sorted() -> None:
    value = {"b": 1, True: 2, None: 3, 7: 4, Decimal("1.50"): 5, DECOMPOSED: DECOMPOSED}
    canonical = canonicalise(value)
    assert canonical == {"1.50": 5, "7": 4, "b": 1, "null": 3, "true": 2, COMPOSED: COMPOSED}
    assert list(canonical) == sorted(canonical)


def test_a_decimal_keeps_its_scale_and_negative_zero_folds_onto_zero() -> None:
    assert render_decimal(Decimal("42.0000")) == "42.0000"
    assert render_decimal(Decimal("-0.00")) == "0.00"
    assert canonicalise([Decimal("1E+2")]) == ["100"]
    assert coerce_key(False) == "false"


@pytest.mark.parametrize(
    "value, code",
    [
        pytest.param(0.5, "FLX-1001", id="float"),
        pytest.param({1.5: 1}, "FLX-1001", id="float-key"),
        pytest.param(JCS_MAX_SAFE_INTEGER + 1, "FLX-1003", id="past-2^53"),
        pytest.param({frozenset(): 1}, "FLX-1004", id="key-class"),
        pytest.param(b"x", "FLX-1005", id="bytes"),
        pytest.param({1, 2}, "FLX-1005", id="set"),
        pytest.param(Decimal("NaN"), "FLX-1006", id="nan"),
        pytest.param({COMPOSED: 1, DECOMPOSED: 2}, "FLX-1007", id="nfc-collision"),
        pytest.param({True: 1, "true": 2}, "FLX-1007", id="coercion-collision"),
    ],
)
def test_what_has_no_canonical_form_is_refused(value: Any, code: str) -> None:
    with pytest.raises(FluxError) as refused:
        canonicalise(value)
    assert refused.value.code.code == code


def test_canonicalising_twice_changes_nothing() -> None:
    once = canonicalise({"a": [Decimal("1.0"), {"b": None}], "c": True, "d": (1, 2)})
    assert canonicalise(once) == once
    assert once["d"] == [1, 2]
