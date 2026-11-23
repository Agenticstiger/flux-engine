# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A periods entry's override texts are bounded and must fit their column, or are refused."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from tests.seam import test_projections as base
from tests.seam.test_projections import bundle, period_with, refused, resolved

pytestmark = pytest.mark.unit

_month = base._month


def _with_json_column() -> Dict[str, Any]:
    expose = _month()
    expose["contract"]["schema"].append({"name": "extra", "type": "JSON", "required": False})
    return expose


def _entry(**overrides: str) -> Dict[str, Any]:
    return period_with(kinds=[{"overrides": overrides}])


@pytest.mark.parametrize(
    "text",
    ["9" * 65, "1" * 4301, "9223372036854775808", "-9223372036854775809"],
    ids=["long", "digit-limit", "above-int64", "below-int64"],
)
def test_an_int_override_that_is_long_or_outside_int64_is_refused(
    tmp_path: Path, text: str
) -> None:
    message = refused(bundle(tmp_path, "int", periods=[_entry(quota=text)]), "FLX-3403")
    assert "quota" in message and len(message) < 400


@pytest.mark.parametrize("text", ["9223372036854775807", "-9223372036854775808", "+7", "0"])
def test_an_int_override_inside_int64_reads(tmp_path: Path, text: str) -> None:
    resolved(bundle(tmp_path, "int-ok", periods=[_entry(quota=text)]))


@pytest.mark.parametrize("text", ["1E+60", "100000000", "1E+8", "-1E+9"])
def test_a_decimal_override_that_does_not_fit_the_precision_is_refused(
    tmp_path: Path, text: str
) -> None:
    message = refused(bundle(tmp_path, "dec", periods=[_entry(price=text)]), "FLX-3403")
    assert "price" in message


@pytest.mark.parametrize("text", ["99999999.99", "-99999999.99", "0.00", "1E+7", "5", "0.5"])
def test_a_decimal_override_that_fits_reads(tmp_path: Path, text: str) -> None:
    resolved(bundle(tmp_path, "dec-ok", periods=[_entry(price=text)]))


@pytest.mark.parametrize("text", ['{"a": 1}', "[" * 2000 + "]" * 2000])
def test_a_json_override_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    monkeypatch.setattr(base, "_month", _with_json_column)
    message = refused(bundle(tmp_path, "json", periods=[_entry(extra=text)]), "FLX-3403")
    assert "extra" in message and len(message) < 400
