# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A record's signature has to say something about who signed it.

``signing_payload`` excludes the whole ``signature`` object from the
signed bytes, and ship then wrote the verification key *into* that object
(``record_document["signature"]["publicKey"] = ...``). The key that
identified the signer was therefore not covered by the signature it
accompanied: anyone could rewrite the body, sign it with a key of their
own, drop that key's public half in the same field, and the record still
verified against the only key travelling with it.

There was no trust anchor anywhere either. ``verify_record`` took whatever
key the caller handed it, and the only in-repo caller handed it the public
key derived from the private key it had signed with one line earlier -- so
the gate's ``record.signature`` check was structurally incapable of
failing.

The fix is two things, and this module holds both to their claims: the
signer identity lives in a top-level ``signer`` block inside the signed
body, and ``verify_record`` checks against trust anchors its caller
supplies.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping

import pytest

from flux_engine.concordance.record import (
    Ed25519Signer,
    SyntheticReference,
    build_record,
    record_signer_public_key,
    signer_block,
    signing_payload,
    verify_record,
)
from flux_engine.concordance.verifier import ClearStatistic

pytestmark = pytest.mark.unit

OPERATOR_KEY = bytes(range(32))
ATTACKER_KEY = bytes(range(64, 96))


@pytest.fixture()
def operator() -> Ed25519Signer:
    return Ed25519Signer(private_key=OPERATOR_KEY, key_id="flux-ship-payment_recovery_moment")


@pytest.fixture()
def record(compiled, committed, committed_rows, operator) -> Mapping[str, Any]:
    evaluation = ClearStatistic(
        compiled=compiled, committed=committed, synthetic_rows=committed_rows
    ).evaluate()
    return build_record(
        compiled=compiled,
        committed=committed,
        evaluation=evaluation,
        synthetic=SyntheticReference(
            output_digest="sha256:" + "a" * 64,
            rows=len(committed_rows),
            generator_digest="sha256:" + "b" * 64,
        ),
        product_ref="example.gold.recovery_moment",
        fluid_version="^0.7.3",
        contract_digest="sha256:" + "c" * 64,
        check_ordinal=1,
        signer=operator,
    ).document


def test_a_genuine_record_verifies_against_the_operators_key(record, operator) -> None:
    """The positive control."""
    assert verify_record(record, operator.public_key) == []


def test_the_signer_block_is_inside_the_signed_bytes(record) -> None:
    assert "signer" in json.loads(signing_payload(record)[len("flux-fidelity-record/1") + 1 :])
    assert (
        record_signer_public_key(record)
        == Ed25519Signer(private_key=OPERATOR_KEY, key_id="_").public_key
    )


def test_a_forged_record_does_not_verify_against_the_operators_key(record, operator) -> None:
    """The whole finding, in one test.

    Rewrite the committed-state counts, re-sign with an attacker key, and
    write that key's identity into the ``signer`` block. The document is
    internally consistent -- it verifies against the key it names about
    itself -- and it must still fail against the anchor a reader holds.
    """
    attacker = Ed25519Signer(private_key=ATTACKER_KEY, key_id=str(record["signature"]["keyId"]))
    forged: Dict[str, Any] = json.loads(json.dumps(dict(record)))
    forged["committedState"]["recordCount"] = 999999
    forged["signer"] = signer_block(attacker, ephemeral=False)
    forged["signature"] = {
        "alg": attacker.alg,
        "keyId": attacker.key_id,
        "sig": attacker.sign(signing_payload(forged)),
    }

    # Self-consistent, which is all a record can say about itself...
    assert verify_record(forged, attacker.public_key) == []
    # ...and worth nothing against the key the reader actually trusts.
    assert verify_record(forged, operator.public_key) == [
        "the record's signature does not verify against any trusted key"
    ]


def test_a_swapped_signer_block_invalidates_the_signature(record) -> None:
    """Rewriting only the identity, without re-signing, must not verify."""
    attacker = Ed25519Signer(private_key=ATTACKER_KEY, key_id="flux-ship-payment_recovery_moment")
    swapped: Dict[str, Any] = json.loads(json.dumps(dict(record)))
    swapped["signer"] = signer_block(attacker, ephemeral=False)
    assert verify_record(swapped, attacker.public_key) != []


def test_several_trust_anchors_are_accepted(record, operator) -> None:
    """An operator with more than one key in rotation is not forced to guess."""
    other = Ed25519Signer(private_key=ATTACKER_KEY, key_id="other").public_key
    assert verify_record(record, [other, operator.public_key]) == []
    assert verify_record(record, [other]) != []


def test_a_signer_block_naming_a_different_key_id_is_refused(record) -> None:
    """The two places a key id appears have to agree, or neither means anything."""
    mismatched: Dict[str, Any] = json.loads(json.dumps(dict(record)))
    mismatched["signer"] = {**mismatched["signer"], "keyId": "somebody-else"}
    assert verify_record(mismatched, record_signer_public_key(record)) == [
        "the record's signer block and its signature name different keys"
    ]
