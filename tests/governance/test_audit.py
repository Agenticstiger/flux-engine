# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``flux_engine.governance.audit``: the decision audit trail.

Every audited decision must validate against the published RFC-04 decision
contract, decisions.ndjson lines must be the decision object *verbatim*
(no meta fields folded in), the parallel meta file must carry exactly the
task's named field set, decisionsDigest must be a genuine raw-bytes digest
that moves when the content moves, and every recorded decision must also
be representable as the KERNEL-SPEC section 10 ``decision`` trace op.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.core.digest import DigestDomain
from flux_engine.core.errors import FluxError
from flux_engine.core.trace import OP_FIELDS, TraceOp
from flux_engine.governance.audit import (
    META_FIELDS,
    DecisionAuditLog,
    DecisionMeta,
    decision_schema_validator,
)
from flux_engine.governance.gate import AIGate


def _allow_decision(gate: AIGate) -> Dict[str, Any]:
    return gate.evaluate(
        {"allowedModels": ["m"], "tokenBudget": 100, "purposeLimitation": False},
        {"model": "m", "useCase": "u", "tokens": 5},
    )


def _deny_decision(gate: AIGate) -> Dict[str, Any]:
    return gate.evaluate(
        {"allowedModels": ["m"], "tokenBudget": 100},
        {"model": "not-m", "useCase": "u", "tokens": 5, "purpose": "p"},
    )


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_every_vector_decision_validates_against_the_published_schema(vectors) -> None:
    validator = decision_schema_validator()
    for vector in vectors:
        gate = AIGate()
        decision = gate.evaluate(vector["policy"], vector["request"], vector.get("skill"))
        errors = list(validator.iter_errors(decision))
        assert errors == [], f"{vector['name']}: {[e.message for e in errors]}"


def test_record_rejects_a_hand_mutated_decision_with_flx_4003() -> None:
    log = DecisionAuditLog()
    broken = {"allow": True, "reasonCode": "ALLOWED"}  # missing required fields
    with pytest.raises(FluxError) as excinfo:
        log.record(broken, run_id="r1", document_id="d1", tick_ms=0, phase="SIGNAL")
    assert excinfo.value.code.code == "FLX-4003"


def test_record_rejects_a_self_contradictory_decision() -> None:
    log = DecisionAuditLog()
    contradiction = {
        "allow": True,
        "reasonCode": "MODEL_NOT_ALLOWED",
        "reason": "x",
        "policyDigest": "sha256:" + "0" * 64,
        "request": {},
    }
    with pytest.raises(FluxError) as excinfo:
        log.record(contradiction, run_id="r1", document_id="d1", tick_ms=0, phase="SIGNAL")
    assert excinfo.value.code.code == "FLX-4003"


# ---------------------------------------------------------------------------
# decisions.ndjson is the decision VERBATIM; meta.ndjson carries the context
# ---------------------------------------------------------------------------


def test_decisions_ndjson_line_is_the_decision_object_verbatim() -> None:
    gate = AIGate()
    decision = _allow_decision(gate)
    log = DecisionAuditLog()
    log.record(decision, run_id="run-1", document_id="doc-1", tick_ms=3600000, phase="SIGNAL")

    line = log.decisions_bytes().decode("utf-8").rstrip("\n")
    assert json.loads(line) == decision
    # none of the meta fields leaked onto the decision line
    for meta_only_field in ("seq", "runId", "documentId", "skillRef", "tickMs", "phase"):
        assert meta_only_field not in json.loads(line)


def test_meta_ndjson_line_carries_exactly_the_named_field_set() -> None:
    gate = AIGate()
    decision = _allow_decision(gate)
    log = DecisionAuditLog()
    log.record(
        decision,
        run_id="run-1",
        document_id="doc-1",
        tick_ms=7_200_000,
        phase="SIGNAL",
        skill_ref="trait-synth",
    )
    line = json.loads(log.meta_bytes().decode("utf-8").rstrip("\n"))
    assert set(line) == set(META_FIELDS)
    assert line == {
        "seq": 0,
        "runId": "run-1",
        "documentId": "doc-1",
        "skillRef": "trait-synth",
        "tickMs": 7_200_000,
        "phase": "SIGNAL",
    }


def test_skill_ref_defaults_to_the_decisions_own_skillref() -> None:
    gate = AIGate()
    decision = gate.evaluate(
        {"allowedModels": ["m"], "tokenBudget": 100, "purposeLimitation": False},
        {"model": "m", "useCase": "u", "tokens": 5},
        {"name": "trait-synth", "tokenBudget": 50},
    )
    assert decision.get("skillRef") == "trait-synth"
    log = DecisionAuditLog()
    entry = log.record(decision, run_id="r1", document_id="d1", tick_ms=0, phase="SIGNAL")
    assert entry.meta.skill_ref == "trait-synth"


def test_seq_is_assigned_by_the_log_never_by_the_caller() -> None:
    gate = AIGate()
    log = DecisionAuditLog()
    for _ in range(3):
        entry = log.record(
            _allow_decision(gate), run_id="r1", document_id="d1", tick_ms=0, phase="SIGNAL"
        )
    assert [e.meta.seq for e in log.entries] == [0, 1, 2]
    assert entry.meta.seq == 2
    assert len(log) == 3


def test_the_schema_falls_back_to_vendored_when_flux_specs_own_path_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """flux_spec is importable but its resolved schema_dir() lacks the file."""
    real_paths = pytest.importorskip(
        "flux_spec._paths", reason="exercises the flux_spec-present branch only"
    )

    from flux_engine.governance import audit as audit_module

    audit_module._decision_schema.cache_clear()
    audit_module.decision_schema_validator.cache_clear()
    monkeypatch.setattr(
        real_paths, "schema_dir", lambda: __import__("pathlib").Path("/nonexistent")
    )
    try:
        contract = audit_module._decision_schema()
        assert "$defs" in contract
    finally:
        audit_module._decision_schema.cache_clear()
        audit_module.decision_schema_validator.cache_clear()


def test_the_schema_is_resolved_via_the_vendored_fallback_when_flux_spec_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    from flux_engine.governance import audit as audit_module

    audit_module._decision_schema.cache_clear()
    audit_module.decision_schema_validator.cache_clear()
    monkeypatch.setitem(sys.modules, "flux_spec._paths", None)  # forces ImportError on import
    try:
        contract = audit_module._decision_schema()
        assert "$defs" in contract
        validator = audit_module.decision_schema_validator()
        assert validator.is_valid(_allow_decision(AIGate()))
    finally:
        audit_module._decision_schema.cache_clear()
        audit_module.decision_schema_validator.cache_clear()


# ---------------------------------------------------------------------------
# decisionsDigest: raw-bytes, moves when content moves
# ---------------------------------------------------------------------------


def test_decisions_digest_is_the_raw_bytes_domain() -> None:
    log = DecisionAuditLog()
    log.record(_allow_decision(AIGate()), run_id="r1", document_id="d1", tick_ms=0, phase="SIGNAL")
    digest = log.decisions_digest()
    assert digest.domain is DigestDomain.RAW_BYTES


def test_a_denied_call_changes_decisionsdigest() -> None:
    gate = AIGate()
    log_allow_only = DecisionAuditLog()
    log_allow_only.record(
        _allow_decision(gate), run_id="r1", document_id="d1", tick_ms=0, phase="SIGNAL"
    )

    log_with_denial = DecisionAuditLog()
    log_with_denial.record(
        _allow_decision(gate), run_id="r1", document_id="d1", tick_ms=0, phase="SIGNAL"
    )
    log_with_denial.record(
        _deny_decision(gate), run_id="r1", document_id="d1", tick_ms=1, phase="SIGNAL"
    )

    assert log_allow_only.decisions_digest() != log_with_denial.decisions_digest()


def test_decisions_digest_is_deterministic_for_the_same_content() -> None:
    gate = AIGate()
    decision = _allow_decision(gate)

    log_a = DecisionAuditLog()
    log_a.record(decision, run_id="r1", document_id="d1", tick_ms=0, phase="SIGNAL")
    log_b = DecisionAuditLog()
    log_b.record(dict(decision), run_id="r1", document_id="d1", tick_ms=0, phase="SIGNAL")

    assert log_a.decisions_digest() == log_b.decisions_digest()


def test_write_produces_files_matching_the_in_memory_bytes(tmp_path: Path) -> None:
    gate = AIGate()
    log = DecisionAuditLog()
    log.record(_allow_decision(gate), run_id="r1", document_id="d1", tick_ms=0, phase="SIGNAL")
    log.record(_deny_decision(gate), run_id="r1", document_id="d1", tick_ms=1, phase="DETECT")

    result = log.write(tmp_path / "evidence")
    assert result.decisions_path.read_bytes() == log.decisions_bytes()
    assert result.meta_path.read_bytes() == log.meta_bytes()
    assert result.decisions_digest == log.decisions_digest()
    assert len(result.decisions_path.read_text().splitlines()) == 2
    assert len(result.meta_path.read_text().splitlines()) == 2


def test_write_creates_the_directory_if_missing(tmp_path: Path) -> None:
    log = DecisionAuditLog()
    log.record(_allow_decision(AIGate()), run_id="r1", document_id="d1", tick_ms=0, phase="SIGNAL")
    target = tmp_path / "does" / "not" / "exist" / "yet"
    result = log.write(target)
    assert result.decisions_path.exists()


# ---------------------------------------------------------------------------
# flux-trace/1 'decision' op
# ---------------------------------------------------------------------------


def test_trace_records_match_the_kernel_specs_decision_op_field_set() -> None:
    gate = AIGate()
    log = DecisionAuditLog()
    log.record(_allow_decision(gate), run_id="r1", document_id="d1", tick_ms=0, phase="SIGNAL")
    records = log.trace_records()
    assert len(records) == 1
    record = records[0]
    assert record["op"] == TraceOp.DECISION.value
    assert set(record) == {"op"} | set(OP_FIELDS[TraceOp.DECISION])


def test_trace_records_carry_the_decisions_own_verdict_fields() -> None:
    gate = AIGate()
    decision = _deny_decision(gate)
    log = DecisionAuditLog()
    log.record(decision, run_id="r1", document_id="d1", tick_ms=0, phase="SIGNAL")
    (record,) = log.trace_records()
    assert record["allow"] == decision["allow"]
    assert record["reasonCode"] == decision["reasonCode"]
    assert record["policyDigest"] == decision["policyDigest"]
    assert record["seq"] == 0


def test_trace_records_are_in_recording_order() -> None:
    gate = AIGate()
    log = DecisionAuditLog()
    log.record(_allow_decision(gate), run_id="r1", document_id="d1", tick_ms=0, phase="SIGNAL")
    log.record(_deny_decision(gate), run_id="r1", document_id="d1", tick_ms=1, phase="SIGNAL")
    seqs = [r["seq"] for r in log.trace_records()]
    assert seqs == [0, 1]


# ---------------------------------------------------------------------------
# DecisionMeta validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(seq=-1, run_id="r", document_id="d", skill_ref=None, tick_ms=0, phase="SIGNAL"),
        dict(seq=0, run_id="", document_id="d", skill_ref=None, tick_ms=0, phase="SIGNAL"),
        dict(seq=0, run_id="r", document_id="", skill_ref=None, tick_ms=0, phase="SIGNAL"),
        dict(seq=0, run_id="r", document_id="d", skill_ref="  ", tick_ms=0, phase="SIGNAL"),
        dict(seq=0, run_id="r", document_id="d", skill_ref=None, tick_ms=-1, phase="SIGNAL"),
        dict(seq=0, run_id="r", document_id="d", skill_ref=None, tick_ms=0, phase=1.5),
        dict(seq=0, run_id="r", document_id="d", skill_ref=None, tick_ms=0, phase=""),
        dict(seq=True, run_id="r", document_id="d", skill_ref=None, tick_ms=0, phase="SIGNAL"),
        dict(seq=0, run_id="r", document_id="d", skill_ref=None, tick_ms=True, phase="SIGNAL"),
    ],
)
def test_decision_meta_rejects_malformed_fields(kwargs: Dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        DecisionMeta(**kwargs)


def test_decision_meta_accepts_an_integer_phase() -> None:
    # Callers may pass core.clock.Phase's own int value instead of its name;
    # this module does not import core.clock, so either spelling is valid.
    meta = DecisionMeta(seq=0, run_id="r", document_id="d", skill_ref=None, tick_ms=0, phase=0)
    assert meta.as_dict()["phase"] == 0
