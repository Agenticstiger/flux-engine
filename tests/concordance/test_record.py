# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""SEAM-FIDELITY-SPEC §6: the fidelity record and its signature."""

from __future__ import annotations

import copy
import json
from typing import Any, Dict

import pytest

from flux_engine.concordance.record import (
    RECORD_VERSION,
    SCOPE_CERTIFIES,
    SCOPE_DOES_NOT_CERTIFY,
    Ed25519Signer,
    SyntheticReference,
    UnsignedFallbackSigner,
    build_record,
    load_record,
    signing_payload,
    verify_record,
)
from flux_engine.concordance.verifier import ClearStatistic
from flux_engine.core.canonical import canonical_bytes
from flux_engine.core.errors import FluxError

pytestmark = pytest.mark.unit

SYNTHETIC = SyntheticReference(
    output_digest="sha256:" + "a" * 64,
    rows=8,
    generator_digest="sha256:" + "b" * 64,
)


def _evaluation(compiled, committed, rows):
    return ClearStatistic(compiled=compiled, committed=committed, synthetic_rows=rows).evaluate()


def _record(compiled, committed, rows, signer, **overrides):
    kwargs: Dict[str, Any] = {
        "compiled": compiled,
        "committed": committed,
        "evaluation": _evaluation(compiled, committed, rows),
        "synthetic": SYNTHETIC,
        "product_ref": "example.gold.recovery_moment",
        "fluid_version": "^0.7.3",
        "contract_digest": "sha256:" + "c" * 64,
        "check_ordinal": 1,
        "signer": signer,
    }
    kwargs.update(overrides)
    return build_record(**kwargs)


# ---------------------------------------------------------------------------
# The shape
# ---------------------------------------------------------------------------


def test_a_record_carries_every_key_the_spec_names(
    compiled, committed, committed_rows, signer
) -> None:
    document = _record(compiled, committed, committed_rows, signer).document
    assert set(document) == {
        "recordVersion",
        "reference",
        "H",
        "G",
        "committedState",
        "synthetic",
        "properties",
        "proofSystem",
        "scope",
        "columnsCovered",
        "signature",
        # The signer identity lives inside the signed body, not beside
        # the signature -- see record.py's docstring.
        "signer",
    }
    assert document["recordVersion"] == RECORD_VERSION
    assert set(document["reference"]) == {
        "productRef",
        "exposeId",
        "fluidVersion",
        "contractDigest",
    }
    assert set(document["synthetic"]) == {"outputDigest", "rows", "generatorDigest"}
    assert set(document["committedState"]) == {
        "ordinal",
        "C1",
        "C2",
        "recordCount",
        "committedAt",
        "checkOrdinal",
    }


def test_a_record_binds_to_the_compiled_digests(
    compiled, committed, committed_rows, signer
) -> None:
    record = _record(compiled, committed, committed_rows, signer)
    assert record.h == compiled.h.value
    assert record.g == compiled.g.value
    assert record.expose_id == compiled.block.expose_id


def test_a_record_names_the_proof_system_rather_than_omitting_it(
    compiled, committed, committed_rows, signer
) -> None:
    assert _record(compiled, committed, committed_rows, signer).document["proofSystem"] == "none"


def test_a_record_carries_one_row_per_declared_property(
    compiled, committed, committed_rows, signer
) -> None:
    record = _record(compiled, committed, committed_rows, signer)
    assert len(record.document["properties"]) == len(compiled.properties)
    assert record.outcomes == tuple("within" for _ in compiled.properties)
    assert record.within


def test_a_record_lists_the_columns_the_properties_cover(
    compiled, committed, committed_rows, signer
) -> None:
    record = _record(compiled, committed, committed_rows, signer)
    assert record.document["columnsCovered"] == list(compiled.block.columns_covered)


def test_a_record_can_publish_a_range_instead_of_an_exact_count(
    compiled, committed, committed_rows, signer
) -> None:
    record = _record(compiled, committed, committed_rows, signer, record_range=[1000, 100000])
    state = record.document["committedState"]
    assert state["recordRange"] == [1000, 100000]
    assert "recordCount" not in state


def test_a_descending_record_range_is_refused(compiled, committed, committed_rows, signer) -> None:
    with pytest.raises(FluxError) as caught:
        _record(compiled, committed, committed_rows, signer, record_range=[10, 1])
    assert caught.value.code.code == "FLX-8002"


def test_a_check_ordinal_counts_from_one(compiled, committed, committed_rows, signer) -> None:
    with pytest.raises(FluxError) as caught:
        _record(compiled, committed, committed_rows, signer, check_ordinal=0)
    assert caught.value.code.code == "FLX-8002"


def test_a_record_carries_no_generation_timestamp(
    compiled, committed, committed_rows, signer
) -> None:
    """The only instant in a record is the owner's commitment time."""
    rendered = json.dumps(dict(_record(compiled, committed, committed_rows, signer).document))
    assert "generated_at" not in rendered
    assert "generatedAt" not in rendered


def test_a_record_canonicalises(compiled, committed, committed_rows, signer) -> None:
    assert canonical_bytes(dict(_record(compiled, committed, committed_rows, signer).document))


def test_two_records_over_one_check_are_byte_identical(
    compiled, committed, committed_rows, signer
) -> None:
    first = _record(compiled, committed, committed_rows, signer).document
    second = _record(compiled, committed, committed_rows, signer).document
    assert canonical_bytes(dict(first)) == canonical_bytes(dict(second))


def test_a_second_check_of_one_state_is_a_different_document(
    compiled, committed, committed_rows, signer
) -> None:
    first = _record(compiled, committed, committed_rows, signer, check_ordinal=1)
    second = _record(compiled, committed, committed_rows, signer, check_ordinal=2)
    assert first.document != second.document


# ---------------------------------------------------------------------------
# The scope statement
# ---------------------------------------------------------------------------


def test_the_scope_statement_is_the_generated_one(
    compiled, committed, committed_rows, signer
) -> None:
    scope = _record(compiled, committed, committed_rows, signer).document["scope"]
    assert scope["certifies"] == SCOPE_CERTIFIES
    assert tuple(scope["doesNotCertify"]) == SCOPE_DOES_NOT_CERTIFY
    assert len(SCOPE_DOES_NOT_CERTIFY) == 6


def test_build_record_takes_no_argument_that_could_change_the_scope() -> None:
    import inspect

    parameters = set(inspect.signature(build_record).parameters)
    assert not any("scope" in name or "certif" in name for name in parameters)


def test_a_record_whose_scope_was_narrowed_is_unreadable(
    compiled, committed, committed_rows, signer
) -> None:
    document = dict(_record(compiled, committed, committed_rows, signer).document)
    document["scope"] = {
        "certifies": SCOPE_CERTIFIES,
        "doesNotCertify": list(SCOPE_DOES_NOT_CERTIFY[:2]),
    }
    with pytest.raises(FluxError) as caught:
        load_record(document)
    assert caught.value.code.code == "FLX-8002"
    assert verify_record(document, None)


def test_a_record_whose_scope_sentence_was_rewritten_is_unreadable(
    compiled, committed, committed_rows, signer
) -> None:
    document = dict(_record(compiled, committed, committed_rows, signer).document)
    document["scope"] = {
        "certifies": "everything a reader might hope for",
        "doesNotCertify": list(SCOPE_DOES_NOT_CERTIFY),
    }
    with pytest.raises(FluxError):
        load_record(document)


# ---------------------------------------------------------------------------
# Refusing to write a record for a refused check
# ---------------------------------------------------------------------------


def test_no_record_is_written_for_a_check_that_declined(
    compiled, committed, committed_rows, signer
) -> None:
    with pytest.raises(FluxError) as caught:
        _record(compiled, committed, committed_rows[:1], signer)
    assert caught.value.code.code == "FLX-8002"
    assert "fail-closed" in caught.value.detail


def test_a_record_is_written_for_an_outside_outcome(
    compiled, committed, committed_rows, signer
) -> None:
    """An outside outcome is a result; the gate is what withholds over it."""
    drifted = [{**row, "amount": str(6 + (index % 3))} for index, row in enumerate(committed_rows)]
    record = _record(compiled, committed, drifted, signer)
    assert "outside" in record.outcomes
    assert not record.within


def test_an_evaluation_for_another_expose_is_refused(
    compiled, committed, committed_rows, signer
) -> None:
    import dataclasses

    evaluation = dataclasses.replace(
        _evaluation(compiled, committed, committed_rows), expose_id="another"
    )
    with pytest.raises(FluxError):
        _record(compiled, committed, committed_rows, signer, evaluation=evaluation)


# ---------------------------------------------------------------------------
# The signature
# ---------------------------------------------------------------------------


def test_a_signed_record_verifies_against_its_key(
    compiled, committed, committed_rows, signer
) -> None:
    record = _record(compiled, committed, committed_rows, signer)
    assert record.signed
    assert record.document["signature"]["alg"] == "ed25519"
    assert record.document["signature"]["keyId"] == signer.key_id
    assert verify_record(record.document, signer.public_key) == []


def test_a_record_signed_by_another_key_does_not_verify(
    compiled, committed, committed_rows, signer
) -> None:
    other = Ed25519Signer(private_key=bytes(range(64, 96)), key_id="another")
    record = _record(compiled, committed, committed_rows, signer)
    reasons = verify_record(record.document, other.public_key)
    assert reasons == ["the record's signature does not verify against any trusted key"]


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: d.__setitem__("H", "sha256:" + "0" * 64), id="H"),
        pytest.param(lambda d: d.__setitem__("G", "sha256:" + "0" * 64), id="G"),
        pytest.param(lambda d: d["properties"][0].__setitem__("outcome", "outside"), id="outcome"),
        pytest.param(lambda d: d["committedState"].__setitem__("ordinal", 99), id="ordinal"),
        pytest.param(lambda d: d["synthetic"].__setitem__("rows", 1), id="rows"),
        pytest.param(lambda d: d.__setitem__("proofSystem", "something"), id="proofSystem"),
        pytest.param(lambda d: d.__setitem__("columnsCovered", []), id="columnsCovered"),
    ],
)
def test_any_change_to_a_signed_record_breaks_its_signature(
    compiled, committed, committed_rows, signer, mutate
) -> None:
    document = copy.deepcopy(dict(_record(compiled, committed, committed_rows, signer).document))
    document["properties"] = [dict(row) for row in document["properties"]]
    document["committedState"] = dict(document["committedState"])
    document["synthetic"] = dict(document["synthetic"])
    mutate(document)
    assert verify_record(document, signer.public_key) != []


def test_a_field_added_after_signing_breaks_the_signature(
    compiled, committed, committed_rows, signer
) -> None:
    document = dict(_record(compiled, committed, committed_rows, signer).document)
    document["extra"] = "smuggled"
    assert verify_record(document, signer.public_key) != []


def test_the_signed_payload_excludes_only_the_signature(
    compiled, committed, committed_rows, signer
) -> None:
    document = dict(_record(compiled, committed, committed_rows, signer).document)
    without = {key: value for key, value in document.items() if key != "signature"}
    assert signing_payload(document) == signing_payload(without)
    assert signing_payload(document).startswith(b"flux-fidelity-record/1\x00")


def test_an_unsigned_fallback_record_says_it_is_unsigned(
    compiled, committed, committed_rows
) -> None:
    record = _record(compiled, committed, committed_rows, UnsignedFallbackSigner())
    assert not record.signed
    assert record.document["signature"]["alg"] == "unsigned-fallback"
    assert record.document["signature"]["sig"] == ""
    assert verify_record(record.document, None) == ["the record states that it was not signed"]


def test_a_signature_algorithm_this_verifier_does_not_read_is_reported(
    compiled, committed, committed_rows, signer
) -> None:
    document = dict(_record(compiled, committed, committed_rows, signer).document)
    document["signature"] = {**document["signature"], "alg": "rsa-2048"}
    reasons = verify_record(document, signer.public_key)
    assert reasons and "rsa-2048" in reasons[0]


def test_a_missing_public_key_is_reported_rather_than_assumed(
    compiled, committed, committed_rows, signer
) -> None:
    document = _record(compiled, committed, committed_rows, signer).document
    assert verify_record(document, None) == [
        "no trusted key was supplied to check the record's signature against"
    ]


def test_a_public_key_of_the_wrong_length_is_reported(
    compiled, committed, committed_rows, signer
) -> None:
    document = _record(compiled, committed, committed_rows, signer).document
    assert verify_record(document, b"\x00" * 16) == [
        "an ed25519 public key is 32 raw bytes",
        "no trusted key was supplied to check the record's signature against",
    ]


def test_a_signature_that_is_not_base64_is_reported(
    compiled, committed, committed_rows, signer
) -> None:
    document = dict(_record(compiled, committed, committed_rows, signer).document)
    document["signature"] = {**document["signature"], "sig": "not base64!!"}
    assert verify_record(document, signer.public_key) == ["the record's signature is not base64"]


def test_a_signature_block_that_is_not_a_mapping_is_reported(
    compiled, committed, committed_rows, signer
) -> None:
    document = dict(_record(compiled, committed, committed_rows, signer).document)
    document["signature"] = "a string"
    assert verify_record(document, signer.public_key) == [
        "the record's signature block is not a mapping"
    ]


@pytest.mark.parametrize(
    ("key", "key_id"),
    [(b"\x00" * 31, "k"), (b"\x00" * 33, "k"), (b"\x00" * 32, "  ")],
)
def test_a_signer_with_unusable_key_material_is_refused(key, key_id) -> None:
    with pytest.raises(FluxError) as caught:
        Ed25519Signer(private_key=key, key_id=key_id)
    assert caught.value.code.code == "FLX-8002"


def test_the_public_key_is_derived_from_the_private_one(signer) -> None:
    assert len(signer.public_key) == 32
    assert signer.public_key != signer.private_key


# ---------------------------------------------------------------------------
# Reading a record back
# ---------------------------------------------------------------------------


def test_a_record_round_trips_through_load(compiled, committed, committed_rows, signer) -> None:
    document = dict(_record(compiled, committed, committed_rows, signer).document)
    reread = load_record(json.loads(json.dumps(document)))
    assert reread.document == document
    assert reread.to_document() == document


@pytest.mark.parametrize(
    "dropped",
    [
        "recordVersion",
        "reference",
        "H",
        "G",
        "committedState",
        "synthetic",
        "properties",
        "proofSystem",
        "scope",
        "columnsCovered",
        "signature",
    ],
)
def test_a_record_missing_a_key_is_refused(
    compiled, committed, committed_rows, signer, dropped
) -> None:
    document = dict(_record(compiled, committed, committed_rows, signer).document)
    document.pop(dropped)
    with pytest.raises(FluxError) as caught:
        load_record(document)
    assert caught.value.code.code == "FLX-8002"
    assert dropped in caught.value.detail


def test_a_record_of_another_version_is_refused(
    compiled, committed, committed_rows, signer
) -> None:
    document = dict(_record(compiled, committed, committed_rows, signer).document)
    document["recordVersion"] = RECORD_VERSION + 1
    with pytest.raises(FluxError):
        load_record(document)


def test_a_record_whose_property_row_has_no_outcome_is_refused(
    compiled, committed, committed_rows, signer
) -> None:
    document = copy.deepcopy(dict(_record(compiled, committed, committed_rows, signer).document))
    document["properties"] = [{"property": "mean"}]
    with pytest.raises(FluxError) as caught:
        load_record(document)
    assert "outcome" in caught.value.detail


def test_a_record_that_is_not_a_mapping_is_refused() -> None:
    with pytest.raises(FluxError):
        load_record(["not", "a", "record"])  # type: ignore[arg-type]


def test_a_record_whose_scope_is_not_a_mapping_is_refused(
    compiled, committed, committed_rows, signer
) -> None:
    document = dict(_record(compiled, committed, committed_rows, signer).document)
    document["scope"] = "narrow"
    with pytest.raises(FluxError):
        load_record(document)


def test_the_record_is_the_one_the_evidence_pack_names(compiled) -> None:
    from flux_engine.concordance.gate import reverification_commands
    from flux_engine.concordance.record import FidelityRecord

    record = FidelityRecord(document={"reference": {"exposeId": compiled.block.expose_id}})
    commands = reverification_commands(record)
    assert any(
        f"run/fidelity-record-{compiled.block.expose_id}.json" in command for command in commands
    )
