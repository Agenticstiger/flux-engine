# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The ``towers`` vertical: registry, derivation, payloads, arrival, jurisdiction, seam, plan."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, FrozenSet

import pytest
import yaml

from flux_engine.core.errors import FluxError
from flux_engine.core.rng import DrawStream
from flux_engine.expr.types import STRING
from flux_engine.runtime.jurisdiction import jurisdiction_of
from flux_engine.runtime.payloads import PAYLOAD_FAMILIES, payload_families
from flux_engine.runtime.signals import DEFAULT_ARRIVAL_MODEL, arrival_model_for
from flux_engine.seam.contract import compile_expose
from flux_engine.seam.mapping import resolve_column_mapping, source_namespace
from flux_engine.stages.plan import plan
from flux_engine.verticals import emit_aliases, get_pack, pack_names
from flux_engine.verticals.towers import TOWERS, TOWERS_EMIT_ALIASES
from flux_engine.world.derived import Derivation, derivation_for
from tests.flux_examples import skip_or_fail_without_flux_examples

pytestmark = pytest.mark.unit

EXAMPLE = "towers-tenant-renewal"
LIFECYCLES = ("onboarding", "active", "atrisk", "notice", "churned")
MARKETS = ("DZ", "IQ", "JO", "KW", "QA", "TN")
TOWERS_SIGNALS = ("sig-sla-breach", "sig-site-outage", "sig-billing-dispute")

#: dp014 ``worst_outage_cause`` values.
DP014_CAUSES = {
    "MAINS_FAILURE",
    "GENERATOR_FAIL",
    "HIGH_TEMPERATURE",
    "DOOR_OPEN",
    "INTRUSION",
    "FUEL_LEVEL_LOW",
    "BATTERY_LOW",
    "RECTIFIER_FAULT",
    "AVIATION_LIGHT_FAIL",
    "COMMS_LOSS",
}

STREAMS = range(1, 401)


def draw_all(derivation: Derivation, source: DrawStream, lifecycle: str) -> Any:
    """Draw one subject's derived attributes on *source* (see ruff B023)."""
    return derivation.draw(lambda: source, lifecycle=lifecycle)


def draw_on(family: Any, source: DrawStream) -> Any:
    """Draw one value on *source* (see ruff B023)."""
    return family.draw(lambda: source)


def mean_of(lifecycle: str, attribute: str) -> Decimal:
    """The mean of *attribute* over independent, deterministic streams."""
    derivation = derivation_for("towers")
    total = sum(
        Decimal(draw_all(derivation, DrawStream.from_entropy(entropy), lifecycle)[attribute])
        for entropy in STREAMS
    )
    return total / len(STREAMS)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_the_registry_ships_telco_and_towers() -> None:
    assert pack_names() == ("telco", "towers")


def test_get_pack_returns_the_towers_pack() -> None:
    assert get_pack("towers") is TOWERS


def test_an_unknown_vertical_fails_naming_both_shipped_packs() -> None:
    with pytest.raises(KeyError) as raised:
        get_pack("no-such-vertical")
    assert "telco" in str(raised.value)
    assert "towers" in str(raised.value)


def test_the_towers_emit_aliases_are_registered() -> None:
    assert emit_aliases("towers") == TOWERS_EMIT_ALIASES


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def test_the_towers_derivation_matches_the_towers_pack() -> None:
    derivation_for("towers").check_against(get_pack("towers"))


def test_the_towers_derivation_draws_exactly_the_pack_subject_attributes() -> None:
    assert set(derivation_for("towers").names) == set(get_pack("towers").subject_attributes)


def test_the_word_cost_of_a_tower_tenant_does_not_depend_on_its_lifecycle() -> None:
    derivation = derivation_for("towers")
    costs = set()
    for lifecycle in LIFECYCLES:
        source = DrawStream.from_entropy(7)
        draw_all(derivation, source, lifecycle)
        costs.add(source.position)
    assert costs == {derivation.word_cost()}


def test_the_towers_derivation_declares_every_lifecycle_under_test() -> None:
    assert set(LIFECYCLES) <= set(derivation_for("towers").by_lifecycle)


def test_at_risk_and_notice_tenants_draw_more_chronic_breaches_than_active_ones() -> None:
    active = mean_of("active", "chronicSlaBreaches")
    assert mean_of("atrisk", "chronicSlaBreaches") > active
    assert mean_of("notice", "chronicSlaBreaches") > active


def test_at_risk_and_notice_tenants_have_fewer_months_to_lease_expiry_than_active_ones() -> None:
    active = mean_of("active", "monthsToLeaseExpiry")
    assert mean_of("atrisk", "monthsToLeaseExpiry") < active
    assert mean_of("notice", "monthsToLeaseExpiry") < active


def test_onboarding_tenants_draw_fewer_breaches_and_more_months_than_active_ones() -> None:
    assert mean_of("onboarding", "chronicSlaBreaches") < mean_of("active", "chronicSlaBreaches")
    assert mean_of("onboarding", "monthsToLeaseExpiry") > mean_of("active", "monthsToLeaseExpiry")


def test_a_churned_tenant_has_at_most_two_months_left_on_its_lease() -> None:
    derivation = derivation_for("towers")
    for entropy in STREAMS:
        drawn = draw_all(derivation, DrawStream.from_entropy(entropy), "churned")
        assert Decimal(0) <= Decimal(drawn["monthsToLeaseExpiry"]) <= Decimal(2)


# ---------------------------------------------------------------------------
# Payload shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("signal", TOWERS_SIGNALS)
def test_a_towers_signal_draws_exactly_the_fields_the_pack_declares(signal: str) -> None:
    families = payload_families(signal, TOWERS)
    assert set(families) == set(TOWERS.signal_attributes[signal])
    assert list(families) == sorted(families)


@pytest.mark.parametrize("signal", TOWERS_SIGNALS)
def test_every_categorical_level_set_sums_to_one(signal: str) -> None:
    categoricals = [
        family for family in payload_families(signal, TOWERS).values() if hasattr(family, "levels")
    ]
    assert categoricals
    for family in categoricals:
        assert sum(family.levels.values()) == Decimal(1)


@pytest.mark.parametrize("signal", ("sig-sla-breach", "sig-site-outage"))
def test_outage_causes_are_dp014_worst_outage_causes(signal: str) -> None:
    family = payload_families(signal, TOWERS)["cause"]
    assert set(family.levels) <= DP014_CAUSES
    for entropy in STREAMS:
        assert draw_on(family, DrawStream.from_entropy(entropy)) in DP014_CAUSES


def test_a_disputed_charge_is_never_the_unbilled_energy_gap() -> None:
    family = payload_families("sig-billing-dispute", TOWERS)["chargeType"]
    assert "UNBILLED_ENERGY_GAP" not in family.levels
    for entropy in STREAMS:
        assert draw_on(family, DrawStream.from_entropy(entropy)) != "UNBILLED_ENERGY_GAP"


def test_an_outage_affects_at_least_one_site() -> None:
    family = payload_families("sig-site-outage", TOWERS)["sitesAffected"]
    for entropy in STREAMS:
        assert draw_on(family, DrawStream.from_entropy(entropy)) >= Decimal(1)


def test_a_breach_availability_stays_inside_its_declared_interval() -> None:
    family = payload_families("sig-sla-breach", TOWERS)["availabilityPct"]
    for entropy in STREAMS:
        value = draw_on(family, DrawStream.from_entropy(entropy))
        assert Decimal("95.00") <= value <= Decimal("99.89")


def test_no_number_in_the_towers_table_entries_is_a_float() -> None:
    values = [
        leaf
        for signal in TOWERS_SIGNALS
        for family in PAYLOAD_FAMILIES[signal].values()
        for leaf in vars(family).values()
    ]
    assert values
    assert not [leaf for leaf in values if isinstance(leaf, float)]
    for signal in TOWERS_SIGNALS:
        for family in PAYLOAD_FAMILIES[signal].values():
            levels = getattr(family, "levels", {})
            assert not [w for w in levels.values() if isinstance(w, float)]


# ---------------------------------------------------------------------------
# Arrival
# ---------------------------------------------------------------------------


def test_tower_tenant_signals_arrive_nine_days_apart_on_average() -> None:
    assert arrival_model_for("towers").gap_mean_ms == Decimal("777600000")


def test_telco_keeps_the_default_arrival_model() -> None:
    assert arrival_model_for("telco") is DEFAULT_ARRIVAL_MODEL


def test_an_unknown_vertical_gets_the_default_arrival_model() -> None:
    assert arrival_model_for("no-such-vertical") is DEFAULT_ARRIVAL_MODEL


# ---------------------------------------------------------------------------
# Jurisdiction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("market", MARKETS)
def test_every_towers_market_is_its_own_jurisdiction(market: str) -> None:
    assert jurisdiction_of(get_pack("towers"), market) == market


def test_an_unmapped_towers_market_is_refused_rather_than_guessed() -> None:
    with pytest.raises(FluxError) as raised:
        jurisdiction_of(get_pack("towers"), "mkt-unknown")
    assert "FLX-4110" in str(raised.value)


# ---------------------------------------------------------------------------
# Seam mapping against the shipped example contract
# ---------------------------------------------------------------------------


def _example_expose(example: Path) -> Any:
    contract = yaml.safe_load((example / "tenant-renewal-risk.fluid.yml").read_text("utf-8"))
    expose = next(
        entry for entry in contract["exposes"] if entry["exposeId"] == "tenant_renewal_risk_moment"
    )
    return compile_expose(expose)


def _towers_declared() -> FrozenSet[str]:
    return source_namespace(
        subject_attributes=sorted(TOWERS.subject_attributes),
        event_payload_fields=sorted(
            {name for signal in TOWERS_SIGNALS for name in TOWERS.signal_attributes[signal]}
        ),
    )


def test_every_column_of_the_shipped_contract_binds_through_the_towers_aliases() -> None:
    example = skip_or_fail_without_flux_examples(EXAMPLE)
    expose = _example_expose(example)
    mapping = resolve_column_mapping(
        expose, declared_sources=_towers_declared(), aliases=TOWERS_EMIT_ALIASES
    )
    assert [binding.column for binding in mapping.bindings] == list(expose.column_names)
    assert {binding.how for binding in mapping.bindings} == {"alias"}
    assert all(binding.source is not None for binding in mapping.bindings)


def test_every_towers_alias_names_a_declared_source_path() -> None:
    declared = _towers_declared()
    assert set(TOWERS_EMIT_ALIASES.values()) <= declared


def test_the_towers_cause_field_is_a_string() -> None:
    """The alias ``outage_cause`` targets a string payload field."""
    assert TOWERS.signal_attributes["sig-sla-breach"]["cause"] is STRING


# ---------------------------------------------------------------------------
# The example plans
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_the_shipped_towers_example_plans_successfully(tmp_path: Path) -> None:
    """Segment query, detector predicates and journey conditions all type-check."""
    example = skip_or_fail_without_flux_examples(EXAMPLE)
    result = plan(example, state_dir=tmp_path / "state")
    assert result.exit_code == 0, result.stderr
    assert result.error_code is None
