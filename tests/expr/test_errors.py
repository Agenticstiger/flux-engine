# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The FLX-6nnn catalog (KERNEL-SPEC §13, ``fluxsim explain FLX-nnnn``)."""

from __future__ import annotations

import inspect
import pkgutil
import re

import pytest

import flux_engine.expr as expr_package
from flux_engine.expr import errors as errors_module
from flux_engine.expr.errors import (
    CATALOG,
    CatalogEntry,
    EnumLiteralError,
    FluxExprError,
    ParseError,
    ReservedWordError,
    TypeMismatchError,
    UnknownPathError,
    known_attributes_hint,
    register_all,
    with_known_attributes,
)

EXPECTED_CODES = {
    ParseError: "FLX-6001",
    ReservedWordError: "FLX-6002",
    EnumLiteralError: "FLX-6003",
    UnknownPathError: "FLX-6004",
    TypeMismatchError: "FLX-6005",
}


@pytest.mark.unit
def test_every_error_class_carries_its_declared_code() -> None:
    for cls, code in EXPECTED_CODES.items():
        assert cls("x").code == code
        assert cls("x").entry.code == code


@pytest.mark.unit
def test_the_catalog_covers_exactly_the_codes_this_package_raises() -> None:
    """A code raised but not cataloged would print nothing under ``fluxsim explain``."""
    assert set(CATALOG) == set(EXPECTED_CODES.values())
    for code, entry in CATALOG.items():
        assert re.fullmatch(r"FLX-6\d{3}", code)
        assert entry.title and entry.hint


@pytest.mark.unit
def test_no_module_raises_a_code_the_catalog_does_not_declare() -> None:
    """Grep the package for ``FLX-`` literals; every one must be in the catalog."""
    found = set()
    for module in pkgutil.iter_modules(expr_package.__path__):
        source = inspect.getsource(__import__(f"flux_engine.expr.{module.name}", fromlist=["_"]))
        found.update(re.findall(r"FLX-\d{4}", source))
    assert found <= set(CATALOG), found - set(CATALOG)


@pytest.mark.unit
def test_the_message_carries_the_code_and_the_offset() -> None:
    error = ParseError("something", position=7)
    assert str(error).startswith("FLX-6001: something")
    assert "offset 7" in str(error)
    assert str(ParseError("something")) == "FLX-6001: something"


@pytest.mark.unit
def test_every_error_is_a_fluxexprerror() -> None:
    for cls in EXPECTED_CODES:
        assert issubclass(cls, FluxExprError)


@pytest.mark.unit
def test_registering_into_a_shared_registry_is_idempotent_and_refuses_to_shadow() -> None:
    registry: dict = {}
    register_all(registry)
    register_all(registry)
    assert registry == dict(CATALOG)

    hostile = {"FLX-6001": CatalogEntry("FLX-6001", "different", "different")}
    with pytest.raises(ValueError):
        register_all(hostile)


@pytest.mark.unit
def test_importing_the_package_publishes_the_codes_to_the_shared_catalog() -> None:
    """``core.errors``' rule: the module that raises a code registers it on import."""
    core_errors = pytest.importorskip("flux_engine.core.errors")
    assert errors_module.PUBLISHED_TO_CORE_CATALOG is True
    for code, entry in CATALOG.items():
        assert core_errors.is_registered(code)
        published = core_errors.get(code)
        assert published.title == entry.title
        assert published.explanation == entry.hint
        assert published.category == "expression language"


@pytest.mark.unit
def test_publishing_twice_is_harmless() -> None:
    pytest.importorskip("flux_engine.core.errors")
    assert errors_module.publish_to_core_catalog() is True


@pytest.mark.unit
def test_the_local_catalog_stays_authoritative_inside_this_package() -> None:
    """The package explains its own codes with no dependency on the shared registry."""
    assert "flux_engine.core.errors" in errors_module.__doc__
    assert ParseError("x").entry is CATALOG["FLX-6001"]


# ---------------------------------------------------------------------------
# FLX-6004 says what the scope DOES offer
# ---------------------------------------------------------------------------

_KNOWN = ("arpu", "lifecycle", "tenureMonths", "traits.monthlySpend")


@pytest.mark.unit
def test_the_hint_lists_the_attributes_sorted() -> None:
    hint = known_attributes_hint(reversed(_KNOWN))
    assert hint.endswith("known attributes: arpu, lifecycle, tenureMonths, traits.monthlySpend")


@pytest.mark.unit
def test_the_hint_suggests_the_closest_match() -> None:
    assert "did you mean 'tenureMonths'?" in known_attributes_hint(_KNOWN, name="tenure_months")


@pytest.mark.unit
def test_a_name_close_to_nothing_gets_the_list_without_a_suggestion() -> None:
    hint = known_attributes_hint(_KNOWN, name="zzzzzzzz")
    assert "did you mean" not in hint
    assert "known attributes: " in hint


@pytest.mark.unit
def test_an_empty_scope_appends_nothing() -> None:
    assert known_attributes_hint(()) == ""


@pytest.mark.unit
def test_a_long_scope_is_elided_rather_than_dumped() -> None:
    hint = known_attributes_hint(f"attr{index:03d}" for index in range(120))
    assert "+80 more" in hint
    assert "fluxsim expr model" in hint


@pytest.mark.unit
def test_with_known_attributes_keeps_the_code_the_offset_and_the_message() -> None:
    error = UnknownPathError("'tenure_months' is not an attribute of the subject scope", position=3)
    enriched = with_known_attributes(error, _KNOWN)
    assert isinstance(enriched, UnknownPathError)
    assert enriched.code == "FLX-6004"
    assert enriched.position == 3
    assert enriched.message.startswith("'tenure_months' is not an attribute")
    assert "did you mean 'tenureMonths'?" in str(enriched)


@pytest.mark.unit
def test_with_known_attributes_returns_the_error_untouched_for_an_empty_scope() -> None:
    error = UnknownPathError("'x' is not an attribute of this scope")
    assert with_known_attributes(error, ()) is error
