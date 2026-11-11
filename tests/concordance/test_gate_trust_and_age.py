# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Two gate checks that could not fail, and now can.

* ``record.signature`` was verified against a key the caller derived from
  the key it had just signed with. The check is the same shape either
  way; what changed is that :func:`~flux_engine.concordance.gate.run_gate`
  now takes the anchor as an argument and says, in the recorded detail,
  when the anchor was the signing key of the same call -- so the pack
  never carries a check that reads as an attribution it does not make.
* ``committedState.age`` was measured with ``now_ms`` set to the
  commitment's own timestamp, which ship had stamped microseconds
  earlier. The age was identically zero, so ``budget.maxStateAge`` could
  never refuse anything, and the decision recorded a green check nobody
  had performed.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from flux_engine.concordance.gate import GateAction, PackedEmit, run_gate
from flux_engine.concordance.record import (
    Ed25519Signer,
    SyntheticReference,
    build_record,
)
from flux_engine.concordance.verifier import ClearStatistic

pytestmark = pytest.mark.unit

ATTACKER_KEY = bytes(range(64, 96))


def _packed(document, rows) -> PackedEmit:
    columns = list(document["expose"]["columns"])
    header = {
        "exposeId": document["expose"]["exposeId"],
        "columns": columns,
        "contractDigest": "sha256:" + "c" * 64,
    }
    return PackedEmit(
        header=header,
        rows=tuple(tuple(row[name] for name in columns) for row in rows),
    )


@pytest.fixture()
def scene(document, compiled, committed, committed_rows, signer) -> Dict[str, Any]:
    packed = _packed(document, committed_rows)
    evaluation = ClearStatistic(
        compiled=compiled, committed=committed, synthetic_rows=committed_rows
    ).evaluate()
    record = build_record(
        compiled=compiled,
        committed=committed,
        evaluation=evaluation,
        synthetic=SyntheticReference(
            output_digest=packed_digest(packed),
            rows=len(committed_rows),
            generator_digest="sha256:" + "b" * 64,
        ),
        product_ref="example.gold.recovery_moment",
        fluid_version="^0.7.3",
        contract_digest="sha256:" + "c" * 64,
        check_ordinal=1,
        signer=signer,
    )
    return {
        "compiled": compiled,
        "record_document": record.document,
        "committed": committed,
        "packed": packed,
    }


def packed_digest(packed: PackedEmit) -> str:
    from flux_engine.concordance.verifier import emit_stream_digest

    return emit_stream_digest(header=packed.header, rows=packed.rows, subject="outputDigest").value


def test_a_supplied_trust_anchor_verifies_and_is_not_called_self_attested(scene, signer) -> None:
    decision = run_gate(**scene, trusted_keys=signer.public_key)
    check = next(c for c in decision.checks if c.name == "record.signature")
    assert check.passed is True
    assert "self-attested" not in check.detail
    assert decision.action is GateAction.EMIT


def test_a_self_derived_anchor_records_itself_as_self_attested(scene, signer) -> None:
    """The check still passes -- and stops reading as a statement about an operator."""
    decision = run_gate(**scene, trusted_keys=signer.public_key, self_attested=True)
    check = next(c for c in decision.checks if c.name == "record.signature")
    assert check.passed is True
    assert "self-attested" in check.detail
    assert "attributes the record to no operator" in check.detail


def test_another_partys_key_does_not_verify(scene) -> None:
    other = Ed25519Signer(private_key=ATTACKER_KEY, key_id="other").public_key
    decision = run_gate(**scene, trusted_keys=other)
    check = next(c for c in decision.checks if c.name == "record.signature")
    assert check.passed is False
    assert decision.action is GateAction.WITHHOLD


def test_no_clock_reports_the_age_check_unchecked_rather_than_passed(scene, signer) -> None:
    decision = run_gate(**scene, trusted_keys=signer.public_key, now_ms=None)
    age = next(c for c in decision.checks if c.name == "committedState.age")
    assert age.checked is False
    assert age.passed is True, "a control that could not run must not withhold either"
    assert "not checked" in age.detail
    assert age.to_document()["checked"] is False


def test_a_real_clock_makes_the_age_budget_able_to_refuse(scene, signer, block) -> None:
    max_age = block.budget.max_state_age_ms
    assert max_age is not None
    committed_at_ms = scene["committed"].committed_at_ms

    fresh = run_gate(**scene, trusted_keys=signer.public_key, now_ms=committed_at_ms + max_age)
    fresh_age = next(c for c in fresh.checks if c.name == "committedState.age")
    assert fresh_age.checked is True and fresh_age.passed is True

    stale = run_gate(**scene, trusted_keys=signer.public_key, now_ms=committed_at_ms + max_age + 1)
    stale_age = next(c for c in stale.checks if c.name == "committedState.age")
    assert stale_age.checked is True and stale_age.passed is False
    assert stale.action is GateAction.WITHHOLD
