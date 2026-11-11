# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""SEAM-FIDELITY-SPEC §5: the comparisons, and what makes them decline.

The central assertions here compare the fixed-point verdict with a
reference computed from the row values in exact rational arithmetic. That
reference is not a second implementation of the check -- it is the
definition the check is meant to approximate -- so where the two agree,
the grid is doing what it claims, and where they can disagree, the
comparison happens strictly inside the encoding band the epsilon bounds.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Any, Mapping, Sequence

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flux_engine.concordance.boundary.statistic import UNGROUPED
from flux_engine.concordance.predicate import PropertyKind
from flux_engine.concordance.verifier import (
    ClearStatistic,
    NoProofSystem,
    emit_stream_digest,
)
from flux_engine.core.digest import DigestDomain
from flux_engine.core.errors import FluxError
from tests.concordance.conftest import build_committed, build_compiled, vectors_document

pytestmark = pytest.mark.unit

_VECTORS = vectors_document()


def _evaluate(compiled, committed, rows) -> Any:
    return ClearStatistic(compiled=compiled, committed=committed, synthetic_rows=rows).evaluate()


def _property(compiled, kind: PropertyKind, *, grouped: bool = False):
    for entry in compiled.properties:
        if entry.declaration.kind is kind and (entry.declaration.group_by is not None) == grouped:
            return entry
    raise AssertionError(f"no {kind.value} property")


def _outcome(evaluation, property_id: str):
    for outcome in evaluation.outcomes:
        if outcome.property_id == property_id:
            return outcome
    raise AssertionError(property_id)


def _exact_mean(rows: Sequence[Mapping[str, Any]]) -> Fraction:
    values = [Fraction(Decimal(row["amount"])) for row in rows if row["amount"] is not None]
    return Fraction(sum(values), len(values))


# ---------------------------------------------------------------------------
# The identity case, and the reference comparison
# ---------------------------------------------------------------------------


def test_a_synthetic_dataset_equal_to_the_committed_one_is_within(
    compiled, committed, committed_rows
) -> None:
    evaluation = _evaluate(compiled, committed, committed_rows)
    assert evaluation.within
    assert not evaluation.refused
    assert evaluation.outside == ()
    assert len(evaluation.outcomes) == len(compiled.properties)


def test_the_relative_mean_verdict_agrees_with_exact_rational_arithmetic(
    compiled, committed, committed_rows
) -> None:
    """``|m' - m| <= tau'*|m|`` in exact rationals, against the grid's verdict."""
    entry = _property(compiled, PropertyKind.MEAN)
    committed_mean = _exact_mean(committed_rows)
    for shift in ("0", "0.5", "1", "2", "3"):
        rows = [
            {**row, "amount": str(Decimal(row["amount"]) + Decimal(shift))}
            for row in committed_rows
        ]
        if any(Decimal(row["amount"]) > 8 for row in rows):
            continue
        exact_within = abs(_exact_mean(rows) - committed_mean) <= entry.margin * abs(committed_mean)
        got = _outcome(_evaluate(compiled, committed, rows), entry.property_id).within
        assert got == exact_within, f"shift {shift}"


def test_the_total_variation_verdict_agrees_with_exact_rational_arithmetic(
    compiled, committed, committed_rows
) -> None:
    from flux_engine.concordance.boundary.statistic import compute_property_statistic

    entry = _property(compiled, PropertyKind.MARGINAL_HISTOGRAM)
    for amount in ("1", "2", "4", "8"):
        rows = [{**row, "amount": amount} for row in committed_rows]
        committed_group = committed.statistic.properties[entry.property_id].groups[0]
        synthetic_group = compute_property_statistic(rows, entry, compiled.block).groups[0]
        distance = (
            sum(
                abs(Fraction(a, synthetic_group.k) - Fraction(b, committed_group.k))
                for a, b in zip(synthetic_group.bin_counts, committed_group.bin_counts, strict=True)
            )
            / 2
        )
        assert entry.margin is not None
        exact_within = distance <= entry.margin
        got = _outcome(_evaluate(compiled, committed, rows), entry.property_id).within
        assert got == exact_within, f"amount {amount}"


def test_the_support_verdict_counts_members_the_committed_state_does_not_hold(
    compiled, committed, committed_rows
) -> None:
    entry = _property(compiled, PropertyKind.SUPPORT)
    assert _outcome(_evaluate(compiled, committed, committed_rows), entry.property_id).within
    strangers = [{**row, "market": "de"} for row in committed_rows]
    assert not _outcome(_evaluate(compiled, committed, strangers), entry.property_id).within


def test_a_correlation_of_one_against_a_correlation_of_one_is_within(
    compiled, committed, committed_rows
) -> None:
    entry = _property(compiled, PropertyKind.CORRELATION)
    # Both sides move the two columns together, so both correlations are 1.
    rows = [
        {**row, "amount": str(index + 1), "attempts": index + 1}
        for index, row in enumerate(committed_rows)
    ]
    assert _outcome(_evaluate(compiled, committed, rows), entry.property_id).within


def test_a_reversed_correlation_is_outside(compiled, committed, committed_rows) -> None:
    entry = _property(compiled, PropertyKind.CORRELATION)
    rows = [
        {**row, "amount": str(len(committed_rows) - index), "attempts": index + 1}
        for index, row in enumerate(committed_rows)
    ]
    assert not _outcome(_evaluate(compiled, committed, rows), entry.property_id).within


def test_the_quantile_verdict_compares_two_order_statistics(
    compiled, committed, committed_rows
) -> None:
    entry = _property(compiled, PropertyKind.QUANTILE)
    assert entry.margin_units is not None
    nudged = [
        {**row, "amount": str(min(Decimal(row["amount"]) + Decimal("0.0625"), Decimal(8)))}
        for row in committed_rows
    ]
    assert _outcome(_evaluate(compiled, committed, nudged), entry.property_id).within
    far = [{**row, "amount": "8"} for row in committed_rows]
    assert not _outcome(_evaluate(compiled, committed, far), entry.property_id).within


def test_a_grouped_property_is_within_only_when_every_group_is(
    compiled, committed, committed_rows
) -> None:
    entry = _property(compiled, PropertyKind.MEAN, grouped=True)
    rows = [
        {**row, "amount": "8" if row["market"] == committed_rows[0]["market"] else row["amount"]}
        for row in committed_rows
    ]
    outcome = _outcome(_evaluate(compiled, committed, rows), entry.property_id)
    assert not outcome.within
    assert len(outcome.groups) == 2
    assert sorted(group.within for group in outcome.groups) == [False, True]


def test_a_group_outcome_carries_both_counts(compiled, committed, committed_rows) -> None:
    entry = _property(compiled, PropertyKind.MEAN, grouped=True)
    outcome = _outcome(_evaluate(compiled, committed, committed_rows), entry.property_id)
    for group in outcome.groups:
        assert group.committed_count == group.synthetic_count == len(committed_rows) // 2


def test_an_ungrouped_property_reports_one_group_with_the_empty_key(
    compiled, committed, committed_rows
) -> None:
    entry = _property(compiled, PropertyKind.MEAN)
    outcome = _outcome(_evaluate(compiled, committed, committed_rows), entry.property_id)
    assert [group.group for group in outcome.groups] == [UNGROUPED]


# ---------------------------------------------------------------------------
# Fail-closed
# ---------------------------------------------------------------------------


def test_a_refusal_is_not_a_verdict(compiled, committed, committed_rows) -> None:
    """An evaluation carrying a refusal is not within, whatever its outcomes say."""
    rows = [dict(row) for row in committed_rows]
    rows[0]["market"] = "de"
    evaluation = _evaluate(compiled, committed, rows)
    assert evaluation.refused
    assert not evaluation.within
    within_outcomes = [outcome for outcome in evaluation.outcomes if outcome.within]
    assert within_outcomes, "some properties still answered"


def test_a_refusal_names_no_value(compiled, committed, committed_rows) -> None:
    rows = [dict(row) for row in committed_rows]
    rows[3]["amount"] = "999"
    evaluation = _evaluate(compiled, committed, rows)
    assert evaluation.refused
    for refusal in evaluation.refusals:
        assert "999" not in refusal.reason
        assert refusal.code.startswith("FLX-")


def test_a_refusal_on_one_property_leaves_the_others_answerable(
    compiled, committed, committed_rows
) -> None:
    rows = [dict(row) for row in committed_rows]
    rows[0]["market"] = "de"
    evaluation = _evaluate(compiled, committed, rows)
    refused_ids = {refusal.property_id for refusal in evaluation.refusals}
    answered = {outcome.property_id for outcome in evaluation.outcomes} - refused_ids
    assert answered, "the properties over other columns still answered"


def test_a_record_count_outside_the_declared_range_refuses_everything(
    compiled, committed, committed_rows
) -> None:
    evaluation = _evaluate(compiled, committed, committed_rows[:1])
    assert evaluation.refused
    assert evaluation.outcomes == ()


def test_a_committed_state_under_a_different_g_is_refused(
    compiled, committed, committed_rows
) -> None:
    import dataclasses

    moved = dataclasses.replace(committed, g="sha256:" + "0" * 64)
    evaluation = _evaluate(compiled, moved, committed_rows)
    assert [refusal.code for refusal in evaluation.refusals] == ["FLX-5011"]
    assert evaluation.outcomes == ()


def test_a_committed_state_for_a_different_expose_is_refused(
    compiled, committed, committed_rows
) -> None:
    import dataclasses

    moved = dataclasses.replace(committed, expose_id="another")
    evaluation = _evaluate(compiled, moved, committed_rows)
    assert "FLX-5005" in {refusal.code for refusal in evaluation.refusals}


def test_a_committed_state_missing_a_property_is_refused(
    compiled, committed, committed_rows
) -> None:
    import dataclasses
    from types import MappingProxyType

    entry = compiled.properties[0]
    trimmed = dataclasses.replace(
        committed,
        statistic=dataclasses.replace(
            committed.statistic,
            properties=MappingProxyType(
                {
                    key: value
                    for key, value in committed.statistic.properties.items()
                    if key != entry.property_id
                }
            ),
        ),
    )
    evaluation = _evaluate(compiled, trimmed, committed_rows)
    assert entry.property_id in {refusal.property_id for refusal in evaluation.refusals}


def test_a_correlation_below_its_variance_floor_is_refused(
    compiled, committed, committed_rows
) -> None:
    entry = _property(compiled, PropertyKind.CORRELATION)
    flat = [{**row, "amount": "4", "attempts": 4} for row in committed_rows]
    evaluation = _evaluate(compiled, committed, flat)
    assert evaluation.refused
    assert entry.property_id in {refusal.property_id for refusal in evaluation.refusals}
    assert not _outcome(evaluation, entry.property_id).within


# ---------------------------------------------------------------------------
# The record row (§6's shape)
# ---------------------------------------------------------------------------


def test_a_record_row_carries_exactly_the_keys_the_spec_names(
    compiled, committed, committed_rows
) -> None:
    evaluation = _evaluate(compiled, committed, committed_rows)
    for outcome in evaluation.outcomes:
        row = outcome.to_record_row()
        assert set(row) <= {
            "property",
            "column",
            "columns",
            "groupBy",
            "tolerance",
            "margin",
            "epsilon",
            "outcome",
        }
        assert ("column" in row) != ("columns" in row)
        assert row["outcome"] in ("within", "outside")
        assert ("groupBy" in row) == (outcome.group_by is not None)


def test_a_record_row_quotes_the_declared_tolerance(
    compiled, committed, committed_rows, document
) -> None:
    evaluation = _evaluate(compiled, committed, committed_rows)
    declared = document["extension"]["fidelity"]["properties"]
    for outcome, entry in zip(evaluation.outcomes, declared, strict=True):
        assert outcome.to_record_row()["tolerance"] == {
            key: str(value) for key, value in sorted(entry["tolerance"].items())
        }


def test_a_record_row_carries_no_group_detail(compiled, committed, committed_rows) -> None:
    """§6 fixes one row per declared property; the groups stay on the evaluation."""
    evaluation = _evaluate(compiled, committed, committed_rows)
    for outcome in evaluation.outcomes:
        assert "groups" not in outcome.to_record_row()
        assert outcome.groups


# ---------------------------------------------------------------------------
# The claim, and the proof-system port
# ---------------------------------------------------------------------------


def test_the_proof_system_is_named_none(compiled, committed, committed_rows) -> None:
    backend = ClearStatistic(compiled=compiled, committed=committed, synthetic_rows=committed_rows)
    assert backend.proof_system.name == "none"
    assert NoProofSystem().name == "none"


def test_the_backend_satisfies_the_published_extension_contract(
    compiled, committed, committed_rows
) -> None:
    from flux_engine.api.fidelity import FidelityVerifier, ProofSystem

    backend = ClearStatistic(compiled=compiled, committed=committed, synthetic_rows=committed_rows)
    assert isinstance(backend, FidelityVerifier)
    assert isinstance(backend.proof_system, ProofSystem)


def test_a_claim_never_says_agreement_for_a_refused_or_outside_evaluation(
    compiled, committed, committed_rows
) -> None:
    from flux_engine.api.claims import ClaimBasis, ClaimType

    ok = ClearStatistic(
        compiled=compiled, committed=committed, synthetic_rows=committed_rows
    ).check(None)
    assert ok.claim_type is ClaimType.FIDELITY
    assert ok.basis is ClaimBasis.NONE
    assert "is within its compiled margin" in ok.statement

    # Amounts pushed to the top of the range in a varying pattern, so the
    # comparison lands outside rather than refusing on a flat column.
    drifted = [{**row, "amount": str(6 + (index % 3))} for index, row in enumerate(committed_rows)]
    outside = ClearStatistic(compiled=compiled, committed=committed, synthetic_rows=drifted).check(
        None
    )
    assert "outside" in outside.statement
    assert "every one of" not in outside.statement

    refused = ClearStatistic(
        compiled=compiled, committed=committed, synthetic_rows=committed_rows[:1]
    ).check(None)
    assert "declined to answer" in refused.statement


def test_a_claim_carries_the_digests_a_reader_can_retake(
    compiled, committed, committed_rows
) -> None:
    claim = ClearStatistic(
        compiled=compiled, committed=committed, synthetic_rows=committed_rows
    ).check(None)
    assert [ref.value for ref in claim.evidence] == [compiled.h.value, compiled.g.value]
    assert claim.verifier == "ClearStatistic/none"


def test_the_none_backend_refuses_an_attestation(compiled, committed, committed_rows) -> None:
    backend = ClearStatistic(compiled=compiled, committed=committed, synthetic_rows=committed_rows)
    with pytest.raises(FluxError) as caught:
        backend.check(None, b"an artefact")
    assert caught.value.code.code == "FLX-5005"


def test_a_statement_for_a_different_expose_is_refused(compiled, committed, committed_rows) -> None:
    from flux_engine.api.fidelity import FidelityStatement

    backend = ClearStatistic(compiled=compiled, committed=committed, synthetic_rows=committed_rows)
    statement = FidelityStatement(expose_id="another", contract_digest=compiled.h)
    with pytest.raises(FluxError):
        backend.check(statement)
    assert backend.check(
        FidelityStatement(expose_id=compiled.block.expose_id, contract_digest=compiled.h)
    )


# ---------------------------------------------------------------------------
# The recomputed output digest
# ---------------------------------------------------------------------------


def test_the_output_digest_follows_the_kernel_spec_framing() -> None:
    from flux_engine.core.canonical import canonical_bytes, sha256_hex

    header = {"exposeId": "e", "columns": ["a", "b"]}
    rows = [["1", "2"], ["3", "4"]]
    expected = sha256_hex(
        b"flux-emit/1\x00"
        + canonical_bytes(header)
        + b"\x00"
        + b"".join(canonical_bytes(list(row)) + b"\n" for row in rows)
    )
    digest = emit_stream_digest(header=header, rows=rows, subject="d")
    assert digest.value == "sha256:" + expected
    assert digest.domain is DigestDomain.JCS_NDJSON_STREAM


def test_the_output_digest_depends_on_the_row_order() -> None:
    header = {"exposeId": "e", "columns": ["a"]}
    forward = emit_stream_digest(header=header, rows=[["1"], ["2"]], subject="d")
    backward = emit_stream_digest(header=header, rows=[["2"], ["1"]], subject="d")
    assert forward.value != backward.value


def test_the_output_digest_depends_on_the_header() -> None:
    rows = [["1"]]
    first = emit_stream_digest(header={"columns": ["a"]}, rows=rows, subject="d")
    second = emit_stream_digest(header={"columns": ["b"]}, rows=rows, subject="d")
    assert first.value != second.value


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@given(order=st.permutations(range(len(_VECTORS["committedSlice"]))))
@settings(max_examples=20, deadline=None)
@pytest.mark.property
def test_the_verdict_does_not_depend_on_the_order_the_synthetic_rows_arrive_in(
    order,
) -> None:
    document = vectors_document()
    compiled = build_compiled(document)
    committed = build_committed(document, compiled)
    rows = document["committedSlice"]
    reference = _evaluate(compiled, committed, rows)
    shuffled = _evaluate(compiled, committed, [rows[index] for index in order])
    assert [o.outcome for o in shuffled.outcomes] == [o.outcome for o in reference.outcomes]
    assert shuffled.within == reference.within


# ---------------------------------------------------------------------------
# The support rule counts records, not distinct strange values
# ---------------------------------------------------------------------------


def _support_outcome(document, rows):
    compiled = build_compiled(document)
    committed = build_committed(document, compiled)
    evaluation = _evaluate(compiled, committed, rows)
    entry = _property(compiled, PropertyKind.SUPPORT)
    return _outcome(evaluation, entry.property_id)


def _support_document(maximum: int):
    document = vectors_document()
    for declared in document["extension"]["fidelity"]["properties"]:
        if declared["property"] == "support":
            declared["tolerance"]["max"] = maximum
    return document


def test_a_dataset_entirely_off_support_is_outside_under_a_max_of_one() -> None:
    """§5's "count of non-members" is counted in records.

    Every row carrying one undeclared level is 100% of the dataset off the
    committed support. Counting distinct strange values instead would make
    that one non-member and pass it under any ``max >= 1`` -- the tolerance
    would bound the size of the strange set rather than how much of the
    dataset is strange.
    """
    document = _support_document(1)
    rows = [dict(row) for row in document["committedSlice"]]
    for row in rows:
        row["market"] = "zz"
    assert _support_outcome(document, rows).outcome == "outside"


def test_one_off_support_record_is_within_a_max_of_one() -> None:
    """The negative control: a record count of one is still within ``max: 1``."""
    document = _support_document(1)
    rows = [dict(row) for row in document["committedSlice"]]
    rows[0]["market"] = "zz"
    assert _support_outcome(document, rows).outcome == "within"


def test_an_on_support_dataset_is_within_a_max_of_zero() -> None:
    document = _support_document(0)
    rows = [dict(row) for row in document["committedSlice"]]
    assert _support_outcome(document, rows).outcome == "within"
