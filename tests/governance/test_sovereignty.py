# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``flux_engine.governance.sovereignty``: World/Simulation sovereignty.

Covers both enforcement modes named in the schema ($defs/sovereignty),
``--zone``/``FLUX_ZONE`` precedence, the "no declared zone means nothing
to check" resolution the module documents, and the lineage-shaped audit
row produced in ``enforcement: audit`` mode.
"""

from __future__ import annotations

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.governance.sovereignty import (
    FLUX_ZONE_ENV_VAR,
    SovereigntyAuditRow,
    SovereigntyPolicy,
    SovereigntyViolation,
    declared_zone,
    enforce_sovereignty,
)

# ---------------------------------------------------------------------------
# SovereigntyPolicy construction
# ---------------------------------------------------------------------------


def test_from_document_returns_none_when_no_sovereignty_block() -> None:
    assert SovereigntyPolicy.from_document(None) is None


def test_from_document_defaults_enforcement_to_block() -> None:
    policy = SovereigntyPolicy.from_document({"allowedZones": ["eu-west"]})
    assert policy is not None
    assert policy.enforcement == "block"
    assert policy.allowed_zones == ("eu-west",)


def test_from_document_honours_explicit_audit_enforcement() -> None:
    policy = SovereigntyPolicy.from_document({"allowedZones": ["eu-west"], "enforcement": "audit"})
    assert policy is not None
    assert policy.enforcement == "audit"


@pytest.mark.parametrize("bad_enforcement", ["Block", "AUDIT", "monitor", ""])
def test_construction_rejects_an_unknown_enforcement_value(bad_enforcement: str) -> None:
    with pytest.raises(ValueError):
        SovereigntyPolicy(allowed_zones=("eu-west",), enforcement=bad_enforcement)


def test_construction_rejects_empty_allowed_zones() -> None:
    with pytest.raises(ValueError):
        SovereigntyPolicy(allowed_zones=())


# ---------------------------------------------------------------------------
# declared_zone(): --zone / FLUX_ZONE precedence
# ---------------------------------------------------------------------------


def test_cli_zone_wins_over_env() -> None:
    assert declared_zone(cli_zone="eu-west", env={FLUX_ZONE_ENV_VAR: "us-east"}) == "eu-west"


def test_env_is_used_when_no_cli_zone() -> None:
    assert declared_zone(cli_zone=None, env={FLUX_ZONE_ENV_VAR: "us-east"}) == "us-east"


def test_blank_cli_zone_falls_back_to_env() -> None:
    assert declared_zone(cli_zone="   ", env={FLUX_ZONE_ENV_VAR: "us-east"}) == "us-east"


def test_neither_declared_returns_none() -> None:
    assert declared_zone(cli_zone=None, env={}) is None


def test_blank_env_value_is_treated_as_absent() -> None:
    assert declared_zone(cli_zone=None, env={FLUX_ZONE_ENV_VAR: "  "}) is None


def test_default_env_source_reads_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FLUX_ZONE_ENV_VAR, "eu-west")
    assert declared_zone() == "eu-west"
    monkeypatch.delenv(FLUX_ZONE_ENV_VAR)
    assert declared_zone() is None


# ---------------------------------------------------------------------------
# enforce_sovereignty(): block, audit, no-op paths
# ---------------------------------------------------------------------------


def test_no_policy_is_always_compliant() -> None:
    assert (
        enforce_sovereignty(
            None,
            zone="us-east",
            document_id="w1",
            document_kind="World",
            event_time="2026-01-01T00:00:00Z",
        )
        is None
    )


def test_no_declared_zone_does_not_refuse_the_run_but_is_written_down() -> None:
    """The control between "checked and allowed" and "never checked".

    The zone is resolved from a value the runner supplies about itself, so
    omitting --zone/FLUX_ZONE is the one case where an `enforcement: block`
    policy does not run at all. That still does not refuse the run (see the
    module's "Ambiguity resolved" note) -- but it is no longer invisible:
    the receipt and the evidence pack used to be byte-identical to a run
    that HAD been checked and passed.
    """
    for enforcement in ("block", "audit"):
        policy = SovereigntyPolicy(allowed_zones=("eu-west",), enforcement=enforcement)
        row = enforce_sovereignty(
            policy,
            zone=None,
            document_id="w1",
            document_kind="World",
            event_time="2026-01-01T00:00:00Z",
        )
        assert isinstance(row, SovereigntyAuditRow)
        assert row.enforced is False
        assert row.zone is None
        assert row.allowed_zones == ("eu-west",)


def test_a_not_enforced_row_is_not_reported_as_a_violation() -> None:
    """Two facts, two facet names: "somewhere it should not have been" and
    "we do not know where it was" must not arrive under one key."""
    row = enforce_sovereignty(
        SovereigntyPolicy(allowed_zones=("eu-west",)),
        zone=None,
        document_id="w1",
        document_kind="World",
        event_time="2026-01-01T00:00:00Z",
        run_id="run-7",
    )
    assert row is not None
    facets = row.as_lineage_facets()
    assert "sovereigntyViolation" not in facets
    assert facets["sovereigntyNotEnforced"] == {
        "zone": None,
        "allowedZones": ["eu-west"],
        "documentId": "w1",
        "documentKind": "World",
        "runId": "run-7",
    }


def test_no_policy_and_no_zone_still_reports_nothing() -> None:
    """The negative control: nothing declared, nothing to say."""
    assert (
        enforce_sovereignty(
            None,
            zone=None,
            document_id="w1",
            document_kind="World",
            event_time="2026-01-01T00:00:00Z",
        )
        is None
    )


def test_an_allowed_zone_is_compliant_in_both_modes() -> None:
    for enforcement in ("block", "audit"):
        policy = SovereigntyPolicy(allowed_zones=("eu-west", "us-east"), enforcement=enforcement)
        assert (
            enforce_sovereignty(
                policy,
                zone="eu-west",
                document_id="w1",
                document_kind="World",
                event_time="2026-01-01T00:00:00Z",
            )
            is None
        )


def test_block_mode_raises_sovereignty_violation_with_flx_4001() -> None:
    policy = SovereigntyPolicy(allowed_zones=("eu-west",), enforcement="block")
    with pytest.raises(SovereigntyViolation) as excinfo:
        enforce_sovereignty(
            policy,
            zone="us-east",
            document_id="w1",
            document_kind="World",
            event_time="2026-01-01T00:00:00Z",
        )
    assert isinstance(excinfo.value, FluxError)
    assert excinfo.value.code.code == "FLX-4001"
    assert excinfo.value.zone == "us-east"
    assert excinfo.value.allowed_zones == ("eu-west",)


def test_default_enforcement_is_block() -> None:
    policy = SovereigntyPolicy(allowed_zones=("eu-west",))
    with pytest.raises(SovereigntyViolation):
        enforce_sovereignty(
            policy,
            zone="us-east",
            document_id="w1",
            document_kind="World",
            event_time="2026-01-01T00:00:00Z",
        )


def test_audit_mode_returns_a_row_instead_of_raising() -> None:
    policy = SovereigntyPolicy(allowed_zones=("eu-west",), enforcement="audit")
    row = enforce_sovereignty(
        policy,
        zone="us-east",
        document_id="sim-1",
        document_kind="Simulation",
        event_time="2026-01-01T00:00:00Z",
        run_id="run-42",
    )
    assert isinstance(row, SovereigntyAuditRow)
    assert row.enforced is True
    assert row.zone == "us-east"
    assert row.allowed_zones == ("eu-west",)
    assert row.document_id == "sim-1"
    assert row.document_kind == "Simulation"
    assert row.run_id == "run-42"


def test_audit_row_renders_lineage_facets() -> None:
    row = SovereigntyAuditRow(
        zone="us-east",
        allowed_zones=("eu-west",),
        document_id="sim-1",
        document_kind="Simulation",
        event_time="2026-01-01T00:00:00Z",
        run_id="run-42",
    )
    facets = row.as_lineage_facets()
    assert facets["sovereigntyViolation"] == {
        "zone": "us-east",
        "allowedZones": ["eu-west"],
        "documentId": "sim-1",
        "documentKind": "Simulation",
        "runId": "run-42",
    }
