# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The engine and flux-stats agree on what a statistic is.

flux-stats cannot import the engine, so the names it spells for itself are
tied to the engine's grammar here, on the engine's side. Each property kind
the engine parses has exactly one statistic, each tolerance spelling the
engine compiles is one the definitions compare by, and no module of either
package spells a registered identifier a second time. The registry's own
tests, which run without the engine, are in ``packages/flux-stats/tests``.
"""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any, List

import pytest

import flux_engine
import flux_stats
from flux_engine.concordance.predicate import PropertyKind, ToleranceKind
from flux_stats import Check, GroupStatistic, definition_for_kind, definitions

pytestmark = pytest.mark.unit


def _sources() -> List[str]:
    """Every module of both packages, read from where each is imported from."""
    roots = (Path(flux_engine.__file__).parent, Path(flux_stats.__file__).parent)
    return [
        path.read_text(encoding="utf-8") for root in roots for path in sorted(root.rglob("*.py"))
    ]


def test_each_identifier_is_spelled_once_across_the_engine_and_flux_stats() -> None:
    """No second definition elsewhere reuses a registered identifier."""
    sources = _sources()
    for entry in definitions():
        literal = f'"{entry.identifier}"'
        count = sum(source.count(literal) for source in sources)
        assert count == 1, f"{entry.identifier} is spelled {count} times"


def test_each_property_kind_has_exactly_one_statistic_for_each_of_its_forms() -> None:
    """A kind has one statistic, and a quantile that declares bins has a second (ADR 0012)."""
    plain = [entry.kind for entry in definitions() if not entry.binned]
    assert sorted(plain) == sorted(kind.value for kind in PropertyKind)
    assert [(entry.kind, entry.identifier) for entry in definitions() if entry.binned] == [
        (PropertyKind.QUANTILE.value, "quantile/2")
    ]
    for kind in PropertyKind:
        assert definition_for_kind(kind) is definition_for_kind(kind.value)
        assert definition_for_kind(kind).identifier == f"{kind.value}/1"
    assert definition_for_kind(PropertyKind.QUANTILE, binned=True).identifier == "quantile/2"


def _check(**fields: Any) -> Check:
    return Check(subject="p0", **fields)


def test_each_tolerance_the_engine_compiles_is_one_the_definitions_compare_by() -> None:
    """The engine's spellings, passed through, decide a comparison both ways."""
    assert {kind.value for kind in ToleranceKind} == {"absolute", "relative", "distance", "count"}
    mean = definition_for_kind(PropertyKind.MEAN)
    reference = GroupStatistic(group="", k=2, sum_x=20)
    synthetic = GroupStatistic(group="", k=2, sum_x=22)
    absolute = _check(tolerance=ToleranceKind.ABSOLUTE.value, margin_units=1)
    relative = _check(tolerance=ToleranceKind.RELATIVE.value, margin=Fraction(1, 10))
    assert mean.compare(synthetic, reference, absolute) is True
    assert mean.compare(synthetic, reference, replace(absolute, margin_units=0)) is False
    assert mean.compare(synthetic, reference, relative) is True
    assert mean.compare(synthetic, reference, replace(relative, margin=Fraction(1, 20))) is False
    distance = _check(tolerance=ToleranceKind.DISTANCE.value, margin=Fraction(1, 10))
    bins = definition_for_kind(PropertyKind.MARGINAL_HISTOGRAM)
    near = GroupStatistic(group="", k=10, bin_counts=(6, 4))
    far = GroupStatistic(group="", k=10, bin_counts=(8, 2))
    centre = GroupStatistic(group="", k=10, bin_counts=(5, 5))
    assert bins.compare(near, centre, distance) is True
    assert bins.compare(far, centre, distance) is False
    count = _check(tolerance=ToleranceKind.COUNT.value, max_non_members=2)
    support = definition_for_kind(PropertyKind.SUPPORT)
    members = GroupStatistic(group="", k=3, support=("GB", "IE"))
    strange = GroupStatistic(group="", k=4, support=("FR", "IE"), support_counts=(3, 1))
    assert support.compare(strange, members, count) is False
    assert support.compare(strange, members, replace(count, max_non_members=3)) is True
