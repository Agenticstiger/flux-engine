# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""KERNEL-SPEC §13: the whole FLX code catalog, as the engine loads it.

The mechanics of registration (the bands, what ``register`` refuses, what
:class:`FluxError` renders) live with the catalog in flux-stats and are
checked there, in ``packages/flux-stats/tests/test_catalog.py``. Pinned here,
over the catalog the engine loads: that ``flux_engine.core.errors`` is the
shared catalog and not a second one, that every module named as owning
codes can be imported, that every code falls in one of the eight declared
bands, and that no catalog entry uses a word from the register-discipline
list. An error message is a public surface -- it reaches a log, a CI
annotation and a support ticket -- so ``scripts/register_lint.py``'s
vocabulary rule applies to it as much as to the README, even though that
script only scans docs and ``--help``.
"""

from __future__ import annotations

import re

import pytest

import flux_engine.core.errors as engine_errors
import flux_stats.errors as shared_errors
from flux_engine.core.errors import (
    CATEGORIES,
    ErrorCode,
    catalog,
    ensure_core_codes_loaded,
    get,
    is_registered,
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
def test_the_engine_names_the_shared_catalog_rather_than_a_second_one() -> None:
    """One catalog and one exception type, whichever module a caller imports.

    A sink plugin or an engine module that raises ``FluxError`` from
    ``flux_engine.core.errors`` raises the class flux-stats raises, so one
    ``except FluxError`` catches both, and a code either registers is
    explained by the other.
    """
    for name in shared_errors.__all__:
        assert getattr(engine_errors, name) is getattr(shared_errors, name), name
    assert set(engine_errors.__all__) == set(shared_errors.__all__) | {"ensure_core_codes_loaded"}


@pytest.mark.unit
def test_every_registered_code_sits_in_a_declared_band() -> None:
    entries = catalog()
    assert entries, "no codes registered"
    for entry in entries:
        assert entry.band in CATEGORIES
        assert entry.category == CATEGORIES[entry.band]


@pytest.mark.unit
def test_the_core_modules_register_the_codes_they_raise() -> None:
    """The bands the listed modules own are populated after loading, flux-stats's too."""
    for code in ("FLX-1001", "FLX-1002", "FLX-2103", "FLX-5001", "FLX-5002", "FLX-5036"):
        assert is_registered(code)
        assert isinstance(get(code), ErrorCode)


@pytest.mark.unit
def test_every_catalog_module_is_importable() -> None:
    """A module that moved would leave ``fluxsim explain`` unable to explain."""
    ensure_core_codes_loaded()


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
    monkeypatch.setattr(
        engine_errors, "_CORE_CODE_MODULES", ("flux_engine.core.canonical", "flux_engine.core.gone")
    )
    with pytest.raises(ImportError) as excinfo:
        ensure_core_codes_loaded()
    assert "flux_engine.core.gone" in str(excinfo.value)


@pytest.mark.unit
def test_a_missing_distribution_is_reported_as_itself_not_as_a_moved_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``fluxsim explain`` without flux-spec says to install it, not to edit this module."""
    import importlib

    def import_module(name: str, package: object = None) -> object:
        raise ModuleNotFoundError("install flux-spec", name="flux_spec")

    monkeypatch.setattr(importlib, "import_module", import_module)
    with pytest.raises(ModuleNotFoundError, match="install flux-spec") as excinfo:
        ensure_core_codes_loaded()
    assert excinfo.value.name == "flux_spec"


@pytest.mark.unit
def test_the_title_of_a_recorded_plan_refusal_names_each_cause_its_text_lists() -> None:
    """``FLX-2102`` is refused for four causes; a reader of the index sees all four in its title."""
    ensure_core_codes_loaded()
    title = get("FLX-2102").title
    for cause in ("planDigest", "cannot be read", "marker", "shortfall"):
        assert cause in title, title
    for cause in ("edited", "not an object", "historyEndShare", "declaredRows"):
        assert cause in get("FLX-2102").explanation
