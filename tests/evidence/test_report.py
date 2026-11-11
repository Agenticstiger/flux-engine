# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for flux_engine.evidence.report."""

from __future__ import annotations

import pytest

from flux_engine.api.accuracy import AccuracyOutcome, AccuracyVerdict
from flux_engine.api.calibration import Scorecard
from flux_engine.api.claims import ClaimBasis, ClaimType
from flux_engine.api.verdict import Reproducibility, Validity
from flux_engine.core.digest import DigestDomain, DigestRef, digest_of_document
from flux_engine.core.errors import FluxError
from flux_engine.evidence.report import (
    DEFAULT_AUTHORED_DIALS,
    EVIDENCE_SECTIONS,
    build_claims,
    find_banned_words,
    render_evidence_report,
    require_no_banned_words,
)

RUN_DIGEST = DigestRef(
    value="sha256:" + "aa" * 32, domain=DigestDomain.JCS_DOCUMENT, subject="run-digest"
)


def _report(**overrides: object) -> str:
    kwargs = dict(
        bundle_name="telco-payment-recovery",
        kernel_version="0.1.0",
        run_digest=RUN_DIGEST,
        generated_at="2026-01-01T00:00:00.000Z",
        conformance_summary="Every document validated against the FLUX schema.",
        reproducibility=Reproducibility.REPRODUCIBLE,
    )
    kwargs.update(overrides)
    return render_evidence_report(**kwargs)  # type: ignore[arg-type]


@pytest.mark.unit
def test_render_evidence_report_contains_all_five_sections_in_order() -> None:
    report = _report()
    positions = [report.index(f"## {section}") for section in EVIDENCE_SECTIONS]
    assert positions == sorted(positions)
    assert len(positions) == 5


@pytest.mark.unit
def test_render_evidence_report_what_was_not_proven_names_the_authored_dial_by_default() -> None:
    report = _report()
    assert "temperature.campaignResponse" in report
    assert DEFAULT_AUTHORED_DIALS == ("temperature.campaignResponse",)


@pytest.mark.unit
def test_render_evidence_report_states_validity_not_asserted_by_default() -> None:
    report = _report()
    section = report.split("## What Was Not Proven")[1]
    assert "no validity claim" in section.lower()
    # The wording a reader can reproduce: the exact half of the headline
    # `fluxsim verify` prints, not the enum that produced it.
    assert "validity: not asserted" in section.lower()


@pytest.mark.unit
def test_the_evidence_report_never_cites_an_internal_python_symbol() -> None:
    """EVIDENCE.md is handed to a reviewer with no access to the source tree.

    The mandatory disclosure section used to point at
    ``api.verdict.Validity.NOT_ASSERTED`` -- a reference that reader
    cannot resolve. Checked across every validity branch so the next
    template edit cannot reintroduce one.
    """
    for validity in Validity:
        section = _report(validity=validity).split("## What Was Not Proven")[1]
        assert "api.verdict" not in section, section
        assert "Validity." not in section, section
        assert "flux_engine." not in section, section


@pytest.mark.unit
def test_render_evidence_report_raises_flx_8030_on_banned_vocabulary() -> None:
    with pytest.raises(FluxError) as excinfo:
        _report(bundle_name="concordance-demo")
    assert excinfo.value.code.code == "FLX-8030"


@pytest.mark.unit
def test_find_banned_words_is_case_insensitive_and_deduplicating() -> None:
    hits = find_banned_words("This is CERTIFIED and also certified twice, mathematically.")
    assert hits == ("certified", "mathematically")


@pytest.mark.unit
def test_find_banned_words_finds_nothing_in_clean_text() -> None:
    assert find_banned_words("This bundle passed the fidelity check.") == ()


@pytest.mark.unit
def test_require_no_banned_words_passes_clean_text() -> None:
    require_no_banned_words("nothing reserved here")  # does not raise


@pytest.mark.unit
def test_build_claims_always_includes_conformance_and_reproducibility() -> None:
    evidence = (digest_of_document({"a": 1}, subject="x"),)
    claims = build_claims(
        conformance_statement="the bundle validated",
        conformance_evidence=evidence,
        reproducibility=Reproducibility.REPRODUCIBLE,
    )
    types = [c.claim_type for c in claims]
    assert ClaimType.CONFORMANCE in types
    assert ClaimType.REPRODUCIBILITY in types
    assert ClaimType.VALIDITY not in types  # no promoted verdict supplied


@pytest.mark.unit
def test_build_claims_conformance_and_reproducibility_have_basis_none() -> None:
    claims = build_claims(
        conformance_statement="ok",
        conformance_evidence=(),
        reproducibility=Reproducibility.REPRODUCIBLE,
    )
    for claim in claims:
        assert claim.basis is ClaimBasis.NONE


@pytest.mark.unit
def test_build_claims_includes_validity_only_when_a_promoted_verdict_is_given() -> None:
    verdict = AccuracyVerdict(
        prediction_id="pred-1",
        metric_id="churn_rate",
        outcome=AccuracyOutcome.WITHIN_INTERVAL,
        basis=ClaimBasis.HISTORICAL_HOLDOUT,
        scorecard=Scorecard(),
        evidence=(),
    )
    claims = build_claims(
        conformance_statement="ok",
        conformance_evidence=(),
        reproducibility=Reproducibility.REPRODUCIBLE,
        promoted_verdict=verdict,
    )
    validity_claims = [c for c in claims if c.claim_type is ClaimType.VALIDITY]
    assert len(validity_claims) == 1
    assert validity_claims[0].basis is ClaimBasis.HISTORICAL_HOLDOUT


@pytest.mark.unit
def test_build_claims_passes_through_fidelity_claims_unchanged() -> None:
    from flux_engine.api.claims import Claim

    fidelity_claim = Claim(
        claim_type=ClaimType.FIDELITY,
        statement="mean within tolerance",
        basis=ClaimBasis.NONE,
    )
    claims = build_claims(
        conformance_statement="ok",
        conformance_evidence=(),
        reproducibility=Reproducibility.REPRODUCIBLE,
        fidelity_claims=(fidelity_claim,),
    )
    assert fidelity_claim in claims


@pytest.mark.unit
def test_render_evidence_report_supported_validity_section() -> None:
    report = _report(validity=Validity.SUPPORTED)
    section = report.split("## What Was Not Proven")[1]
    assert "a validity claim is made" in section.lower()


@pytest.mark.unit
def test_render_evidence_report_not_supported_validity_section() -> None:
    report = _report(validity=Validity.NOT_SUPPORTED)
    section = report.split("## What Was Not Proven")[1]
    assert "did not hold" in section.lower()


@pytest.mark.unit
def test_render_evidence_report_with_no_authored_dials() -> None:
    report = _report(authored_dials=())
    section = report.split("## What Was Not Proven")[1]
    assert "none" in section.lower()
