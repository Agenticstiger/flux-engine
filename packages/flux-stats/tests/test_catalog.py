# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""KERNEL-SPEC §13: the FLX code catalog, its bands and its one exception type.

The mechanics of registration, checked here without the engine: the eight
bands, what ``register`` refuses, what :class:`FluxError` renders, and that
importing the package registers every code the package raises. The
engine's own ``tests/core/test_errors.py`` checks the whole catalog, the
engine's codes and these together.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import Iterator, Set

import pytest

import flux_stats
import flux_stats.profile  # noqa: F401  (the reader's refusals are numbered too)
from flux_stats import errors
from flux_stats.errors import (
    BAND_SIZE,
    CATEGORIES,
    ErrorCode,
    FluxError,
    band_of,
    catalog,
    category_of,
    explain,
    get,
    is_registered,
    register,
)

pytestmark = pytest.mark.unit

STATS = Path(flux_stats.__file__).resolve().parent

#: The codes this package raises, each registered by the module that raises it.
OWN_CODES = frozenset(
    {
        # The canonical form (flux_stats.canonical) and the exact YAML read
        # (flux_stats.exact_yaml), which moved here from the engine.
        "FLX-1001",
        "FLX-1003",
        "FLX-1004",
        "FLX-1005",
        "FLX-1006",
        "FLX-1007",
        "FLX-3001",
        "FLX-3105",
        "FLX-5001",
        "FLX-5002",
        "FLX-5003",
        "FLX-5004",
        "FLX-5005",
        "FLX-5022",
        "FLX-5030",
        "FLX-5031",
        "FLX-5032",
        "FLX-5033",
        "FLX-5034",
        "FLX-5036",
        "FLX-5038",
    }
)


def test_the_eight_bands_are_the_ones_the_spec_names() -> None:
    assert CATEGORIES == {
        1000: "canonicalisation",
        2000: "plan/replay binding",
        3000: "seam/conformance",
        4000: "governance/sovereignty",
        5000: "numerics",
        6000: "expression language",
        7000: "calibration",
        8000: "evidence/pack",
    }
    assert BAND_SIZE == 1000


@pytest.mark.parametrize(
    ("code", "band"),
    [
        ("FLX-1001", 1000),
        ("FLX-2201", 2000),
        ("FLX-3108", 3000),
        ("FLX-5002", 5000),
        ("FLX-8001", 8000),
    ],
)
def test_band_of_reads_the_thousands_digit(code: str, band: int) -> None:
    assert band_of(code) == band
    assert category_of(code) == CATEGORIES[band]


@pytest.mark.parametrize("code", ["FLX-101", "flx-1001", "FLX1001", "1001", "FLX-10010"])
def test_a_malformed_code_is_refused(code: str) -> None:
    with pytest.raises(ValueError):
        band_of(code)


def test_a_code_outside_the_eight_bands_is_refused() -> None:
    with pytest.raises(ValueError):
        register("FLX-9001", "out of band", "no band nine exists")
    with pytest.raises(ValueError):
        register("FLX-0001", "out of band", "no band zero exists")


_TEST_CODES = ("FLX-7999", "FLX-7998", "FLX-7997", "FLX-7996")


@pytest.fixture(autouse=True)
def _leave_no_test_code_in_the_catalog() -> Iterator[None]:
    """The catalog is process-global, so a code a test registers outlives it.

    These tests register codes in the 7990s to exercise ``register``. Left
    in, they reach everything that reads the whole catalog later in the same
    worker, the engine's generated reference pages among them when the
    engine's suite collects these tests.
    """
    yield
    for code in _TEST_CODES:
        errors._CATALOG.pop(code, None)


def test_registering_the_identical_entry_twice_is_harmless() -> None:
    first = register("FLX-7999", "test entry", "registered by the test suite")
    second = register("FLX-7999", "test entry", "registered by the test suite")
    assert first is second


def test_two_subsystems_cannot_share_one_code() -> None:
    register("FLX-7998", "first claim", "registered by the test suite")
    with pytest.raises(ValueError) as excinfo:
        register("FLX-7998", "second claim", "a different subsystem")
    assert "already registered" in str(excinfo.value)


def test_an_entry_needs_a_title_and_an_explanation() -> None:
    with pytest.raises(ValueError):
        register("FLX-7997", "  ", "explanation")
    with pytest.raises(ValueError):
        register("FLX-7996", "title", "   ")


def test_the_catalog_is_sorted_and_filterable_by_band() -> None:
    codes = [entry.code for entry in catalog()]
    assert codes == sorted(codes)
    numerics = catalog(5000)
    assert {code for code in OWN_CODES if band_of(code) == 5000} <= {
        entry.code for entry in numerics
    }
    assert all(entry.band == 5000 for entry in numerics)


def _unregistered_code() -> str:
    """A well-formed code nothing has claimed, found rather than written as a literal."""
    for number in range(1000, 9000):
        code = f"FLX-{number}"
        if band_of(code) in CATEGORIES and not is_registered(code):
            return code
    raise AssertionError("every code in every band is registered")


def test_get_explains_why_an_unregistered_code_is_missing() -> None:
    with pytest.raises(KeyError) as excinfo:
        get(_unregistered_code())
    assert "not registered" in str(excinfo.value)


def test_explain_prints_code_category_title_and_explanation() -> None:
    text = explain("FLX-5001")
    entry = get("FLX-5001")
    assert entry.code in text
    assert entry.category in text
    assert entry.title in text
    assert entry.explanation in text


def test_flux_error_message_names_the_code_and_the_detail() -> None:
    error = FluxError("FLX-5036", "profile.tables: not an object")
    assert str(error).startswith("FLX-5036: ")
    assert "profile.tables: not an object" in str(error)
    assert error.code.code == "FLX-5036"


def test_flux_error_without_a_detail_is_still_readable() -> None:
    assert str(FluxError(get("FLX-5002"))) == "FLX-5002: " + get("FLX-5002").title


def test_an_entry_knows_its_own_number_and_band() -> None:
    entry = get("FLX-5001")
    assert isinstance(entry, ErrorCode)
    assert entry.number == 5001
    assert entry.band == 5000
    assert entry.category == "numerics"


def _registered_literals() -> Set[str]:
    """Every code a module of the package passes to ``register`` as a literal."""
    found: Set[str] = set()
    for path in sorted(STATS.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", None)) == "register"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and node.args[0].value.startswith("FLX-")
            ):
                found.add(node.args[0].value)
    return found


def test_the_package_registers_exactly_its_own_codes() -> None:
    assert _registered_literals() == OWN_CODES
    for code in sorted(OWN_CODES):
        assert is_registered(code), code


def test_importing_the_package_alone_registers_every_one_of_its_codes() -> None:
    """``import flux_stats`` is enough for ``explain`` to know each of its codes.

    In a fresh interpreter, so that nothing another test imported counts.
    """
    script = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(STATS.parent)!r})\n"
        "import flux_stats\n"
        "from flux_stats.errors import catalog\n"
        "print(json.dumps([entry.code for entry in catalog()]))"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    assert set(json.loads(completed.stdout)) == OWN_CODES
