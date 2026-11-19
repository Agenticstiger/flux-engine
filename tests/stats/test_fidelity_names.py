# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The engine's names for the fidelity-block grammar are flux-stats' objects (ADR 0009).

The grammar and the per-group measurement live in :mod:`flux_stats.fidelity`.
Every engine module and test that imports them from the engine's old homes
gets the same object, so there is one implementation.
"""

from __future__ import annotations

import pytest

from flux_engine.concordance import errors as concordance_errors
from flux_engine.concordance import predicate
from flux_engine.concordance.boundary import statistic
from flux_engine.core.errors import FluxError as EngineFluxError
from flux_engine.seam import contract
from flux_stats import fidelity
from flux_stats.errors import FluxError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("name", sorted(predicate.__all__))
def test_every_name_predicate_exports_is_the_one_in_flux_stats(name: str) -> None:
    assert getattr(predicate, name) is getattr(fidelity, name)


@pytest.mark.parametrize("name", ["FLX_5030", "FLX_5031", "FLX_5032", "FLX_5033", "FLX_5034"])
def test_the_concordance_codes_are_flux_stats_objects(name: str) -> None:
    assert getattr(concordance_errors, name) is getattr(fidelity, name)
    assert name in concordance_errors.__all__


def test_the_seam_names_are_flux_stats_objects() -> None:
    assert contract.FLX_3001 is fidelity.FLX_3001
    assert contract.declared_enum_values is fidelity.declared_enum_values
    assert "FLX_3001" in contract.__all__
    assert "declared_enum_values" in contract.__all__


def test_the_private_key_tuples_a_test_reads_are_flux_stats_objects() -> None:
    for name in (
        "_BLOCK_KEYS",
        "_PROPERTY_KEYS",
        "_TOLERANCE_KEYS",
        "_RECORDS_KEYS",
        "_RANGE_KEYS",
    ):
        assert getattr(predicate, name) is getattr(fidelity, name)


def test_the_boundary_forms_each_group_with_the_function_flux_stats_ships() -> None:
    assert statistic.measure_group is fidelity.measure_group


def test_the_exception_the_grammar_raises_is_the_engines() -> None:
    assert FluxError is EngineFluxError
