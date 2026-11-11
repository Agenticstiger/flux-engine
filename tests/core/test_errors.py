# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""KERNEL-SPEC §13: the FLX code catalog and its bands.

Two things are pinned here beyond the mechanics of registration: that
every code any kernel module registers falls in one of the eight declared
bands, and that no catalog entry uses a word from the register-discipline
list. An error message is a public surface -- it reaches a log, a CI
annotation and a support ticket -- so ``scripts/register_lint.py``'s
vocabulary rule applies to it as much as to the README, even though that
script only scans docs and ``--help``.
"""

from __future__ import annotations

import re
from typing import Iterator

import pytest

from flux_engine.core.errors import (
    BAND_SIZE,
    CATEGORIES,
    ErrorCode,
    FluxError,
    band_of,
    catalog,
    category_of,
    ensure_core_codes_loaded,
    explain,
    get,
    is_registered,
    register,
)

#: Copied from ``scripts/register_lint.py`` -- the same list, applied to
#: the catalog. Kept as a literal here rather than imported so that
#: relaxing the lint script cannot quietly relax this test.
BANNED_WORDS = [
    "concordance",
    "zero-knowledge",
    "tolerance predicate",
    "clear-statistic",
    "practise",
    "mathematically",
    "proves",
    "legally admissible",
    "certified",
    "FLUX Simulator",
]
_BANNED = re.compile("|".join(re.escape(word) for word in BANNED_WORDS), re.IGNORECASE)


@pytest.fixture(autouse=True)
def _all_core_codes_loaded() -> None:
    ensure_core_codes_loaded()


@pytest.mark.unit
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


@pytest.mark.unit
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


@pytest.mark.unit
@pytest.mark.parametrize("code", ["FLX-101", "flx-1001", "FLX1001", "1001", "FLX-10010"])
def test_a_malformed_code_is_refused(code: str) -> None:
    with pytest.raises(ValueError):
        band_of(code)


@pytest.mark.unit
def test_a_code_outside_the_eight_bands_is_refused() -> None:
    with pytest.raises(ValueError):
        register("FLX-9001", "out of band", "no band nine exists")
    with pytest.raises(ValueError):
        register("FLX-0001", "out of band", "no band zero exists")


_TEST_CODES = ("FLX-7999", "FLX-7998", "FLX-7997", "FLX-7996")


@pytest.fixture(autouse=True)
def _leave_no_test_code_in_the_catalog() -> Iterator[None]:
    """The catalog is process-global, so a code a test registers outlives it.

    These tests register codes in the 7990s to exercise ``register``. Left in,
    they reach everything that reads the whole catalog later in the same worker:
    the generated reference pages gain a page for a code that does not exist, and
    which test run fails depends on how xdist placed the modules.
    """
    yield
    from flux_engine.core import errors

    for code in _TEST_CODES:
        errors._CATALOG.pop(code, None)


@pytest.mark.unit
def test_registering_the_identical_entry_twice_is_harmless() -> None:
    first = register("FLX-7999", "test entry", "registered by the test suite")
    second = register("FLX-7999", "test entry", "registered by the test suite")
    assert first is second


@pytest.mark.unit
def test_two_subsystems_cannot_share_one_code() -> None:
    register("FLX-7998", "first claim", "registered by the test suite")
    with pytest.raises(ValueError) as excinfo:
        register("FLX-7998", "second claim", "a different subsystem")
    assert "already registered" in str(excinfo.value)


@pytest.mark.unit
def test_an_entry_needs_a_title_and_an_explanation() -> None:
    with pytest.raises(ValueError):
        register("FLX-7997", "  ", "explanation")
    with pytest.raises(ValueError):
        register("FLX-7996", "title", "   ")


@pytest.mark.unit
def test_every_registered_code_sits_in_a_declared_band() -> None:
    entries = catalog()
    assert entries, "no codes registered"
    for entry in entries:
        assert entry.band in CATEGORIES
        assert entry.category == CATEGORIES[entry.band]


@pytest.mark.unit
def test_the_catalog_is_sorted_and_filterable_by_band() -> None:
    codes = [entry.code for entry in catalog()]
    assert codes == sorted(codes)
    numerics = catalog(5000)
    assert numerics
    assert all(entry.band == 5000 for entry in numerics)


@pytest.mark.unit
def test_the_core_modules_register_the_codes_they_raise() -> None:
    """The bands this agent's modules own are populated after loading."""
    for code in ("FLX-1001", "FLX-1002", "FLX-2103", "FLX-5001", "FLX-5002"):
        assert is_registered(code)
        assert isinstance(get(code), ErrorCode)


def _unregistered_code() -> str:
    """Find a well-formed code no module has claimed.

    Not a literal: every band is being filled in by the modules that
    raise its codes, so a literal here would quietly become a registered
    code one day and stop testing anything.
    """
    for number in range(1000, 9000):
        code = f"FLX-{number}"
        if band_of(code) in CATEGORIES and not is_registered(code):
            return code
    raise AssertionError("every code in every band is registered")


@pytest.mark.unit
def test_get_explains_why_an_unregistered_code_is_missing() -> None:
    with pytest.raises(KeyError) as excinfo:
        get(_unregistered_code())
    assert "not registered" in str(excinfo.value)


@pytest.mark.unit
def test_every_catalog_module_is_importable() -> None:
    """A module that moved would leave ``fluxsim explain`` unable to explain."""
    ensure_core_codes_loaded()


@pytest.mark.unit
def test_explain_prints_code_category_title_and_explanation() -> None:
    text = explain("FLX-1001")
    entry = get("FLX-1001")
    assert entry.code in text
    assert entry.category in text
    assert entry.title in text
    assert entry.explanation in text


@pytest.mark.unit
def test_flux_error_message_names_the_code_and_the_detail() -> None:
    error = FluxError("FLX-1001", "float value in column 'amount'")
    assert str(error).startswith("FLX-1001: ")
    assert "float value in column 'amount'" in str(error)
    assert error.code.code == "FLX-1001"


@pytest.mark.unit
def test_flux_error_without_a_detail_is_still_readable() -> None:
    assert str(FluxError(get("FLX-1002"))) == "FLX-1002: " + get("FLX-1002").title


@pytest.mark.unit
def test_no_catalog_entry_uses_a_word_from_the_register_list() -> None:
    hits = []
    for entry in catalog():
        for field in (entry.title, entry.explanation):
            match = _BANNED.search(field)
            if match:
                hits.append(f"{entry.code}: {match.group(0)!r}")
    assert not hits, f"register-discipline violations in the catalog: {hits}"


@pytest.mark.unit
def test_a_catalog_module_that_moved_is_reported_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loud failure the strict loader exists for."""
    import flux_engine.core.errors as errors_module

    monkeypatch.setattr(
        errors_module, "_CORE_CODE_MODULES", ("flux_engine.core.canonical", "flux_engine.core.gone")
    )
    with pytest.raises(ImportError) as excinfo:
        ensure_core_codes_loaded()
    assert "flux_engine.core.gone" in str(excinfo.value)


@pytest.mark.unit
def test_an_entry_knows_its_own_number_and_band() -> None:
    entry = get("FLX-1001")
    assert entry.number == 1001
    assert entry.band == 1000
    assert entry.category == "canonicalisation"
