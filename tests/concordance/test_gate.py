# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""SEAM-FIDELITY-SPEC §7: the ship gate.

Every check §7 names gets a test that it is run, and a test that breaking
exactly that one thing withholds the bundle while every other check still
passes. A gate whose failures were only ever tested in bulk could be
passing one check by accident.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, FrozenSet, List, Mapping

import pytest

from flux_engine.concordance.compiler import compile_fidelity
from flux_engine.concordance.gate import (
    WITHHELD_EVENT_TYPE,
    GateAction,
    PackedEmit,
    reverification_commands,
    run_gate,
)
from flux_engine.concordance.record import SyntheticReference, build_record
from flux_engine.concordance.verifier import ClearStatistic, emit_stream_digest
from flux_engine.core.canonical import canonical_bytes
from flux_engine.core.errors import FluxError
from tests.concordance.conftest import build_expose

pytestmark = pytest.mark.unit

NOW_MS = 1788220800000 + 24 * 60 * 60 * 1000  # one day after the committed state


def _packed(document: Mapping[str, Any], rows: List[Mapping[str, Any]]) -> PackedEmit:
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


def _record_for(compiled, committed, packed, signer, rows, **overrides):
    evaluation = ClearStatistic(
        compiled=compiled, committed=committed, synthetic_rows=rows
    ).evaluate()
    digest = emit_stream_digest(header=packed.header, rows=packed.rows, subject="outputDigest")
    kwargs: Dict[str, Any] = {
        "compiled": compiled,
        "committed": committed,
        "evaluation": evaluation,
        "synthetic": SyntheticReference(
            output_digest=digest.value,
            rows=len(rows),
            generator_digest="sha256:" + "b" * 64,
        ),
        "product_ref": "example.gold.recovery_moment",
        "fluid_version": "^0.7.3",
        "contract_digest": "sha256:" + "c" * 64,
        "check_ordinal": 1,
        "signer": signer,
    }
    kwargs.update(overrides)
    return build_record(**kwargs)


@pytest.fixture()
def scene(document, compiled, committed, committed_rows, signer):
    packed = _packed(document, committed_rows)
    record = _record_for(compiled, committed, packed, signer, committed_rows)
    return {
        "document": document,
        "compiled": compiled,
        "committed": committed,
        "rows": committed_rows,
        "packed": packed,
        "record": record,
        "signer": signer,
    }


def _run(scene, **overrides):
    kwargs: Dict[str, Any] = {
        "compiled": scene["compiled"],
        "record_document": scene["record"].document,
        "committed": scene["committed"],
        "packed": scene["packed"],
        "now_ms": NOW_MS,
        "trusted_keys": scene["signer"].public_key,
    }
    kwargs.update(overrides)
    return run_gate(**kwargs)


# ---------------------------------------------------------------------------
# The passing case
# ---------------------------------------------------------------------------


def test_a_clean_pack_is_emitted(scene) -> None:
    decision = _run(scene)
    assert decision.action is GateAction.EMIT
    assert decision.code is None
    assert decision.failures == ()
    assert not decision.withheld


def test_every_check_the_spec_names_is_run(scene) -> None:
    names = {check.name for check in _run(scene).checks}
    assert names == {
        "record.shape",
        "binding.H",
        "binding.G",
        "binding.expose",
        "record.signature",
        "synthetic.outputDigest",
        "synthetic.rowCount",
        "committedState.ordinal",
        "committedState.C2",
        "properties.refusals",
        "properties.outcomes",
        "committedState.age",
    }


def test_the_decision_canonicalises(scene) -> None:
    assert canonical_bytes(_run(scene).to_document())


def test_a_passing_decision_carries_the_re_verification_commands(scene) -> None:
    commands = reverification_commands(scene["record"])
    assert commands
    assert all(command.startswith("fluxsim ") for command in commands)


def test_two_runs_of_one_gate_give_one_decision(scene) -> None:
    assert canonical_bytes(_run(scene).to_document()) == canonical_bytes(_run(scene).to_document())


# ---------------------------------------------------------------------------
# One failure at a time
# ---------------------------------------------------------------------------


#: The checks that go red as a consequence of editing the signed body --
#: the signature covers every field of it, so any edit breaks it.
_SIGNED_BODY_EDIT: FrozenSet[str] = frozenset({"record.signature"})


def _assert_only(decision, name: str, *, also: FrozenSet[str] = frozenset()) -> None:
    """*name*, plus *also*, are exactly the checks that failed -- no others.

    ``assert name in failed`` was the whole of this helper, so every one of
    the thirteen single-defect tests below would still have passed under a
    gate that failed every check at once: the one defect class a
    single-defect suite exists to rule out. Equality is what the name
    promised.

    *also* is for the failures one defect genuinely entails, and every use
    of it below says which entailment it is: editing a field of the signed
    body breaks the signature over that body, and moving ``G`` re-scopes
    the properties the gate re-evaluates. Naming them here keeps the
    assertion exact -- a fourteenth check going red is still a failure --
    rather than loosening it to "at least these".
    """
    failed = {check.name for check in decision.failures}
    expected = {name, *also}
    assert failed == expected, f"expected exactly {expected} to fail; failures were {failed}"
    assert decision.action is GateAction.WITHHOLD
    assert decision.code == "FLX-8001"


def test_a_record_bound_to_another_seam_digest_withholds(scene) -> None:
    document = dict(scene["record"].document)
    document["H"] = "sha256:" + "0" * 64
    decision = _run(scene, record_document=document)
    _assert_only(decision, "binding.H", also=_SIGNED_BODY_EDIT)


def test_a_record_bound_to_another_g_withholds(scene) -> None:
    document = dict(scene["record"].document)
    document["G"] = "sha256:" + "0" * 64
    _assert_only(_run(scene, record_document=document), "binding.G", also=_SIGNED_BODY_EDIT)


def test_a_committed_state_under_another_g_withholds(scene) -> None:
    import dataclasses

    moved = dataclasses.replace(scene["committed"], g="sha256:" + "0" * 64)
    decision = _run(scene, committed=moved)
    # Moving the committed state's G re-scopes the properties the gate
    # re-evaluates against it, so both property checks go with it.
    _assert_only(
        decision, "binding.G", also=frozenset({"properties.outcomes", "properties.refusals"})
    )


def test_a_record_for_another_expose_withholds(scene) -> None:
    document = copy.deepcopy(dict(scene["record"].document))
    document["reference"] = {**document["reference"], "exposeId": "another"}
    _assert_only(_run(scene, record_document=document), "binding.expose", also=_SIGNED_BODY_EDIT)


def test_an_unsigned_record_withholds(document, compiled, committed, committed_rows) -> None:
    from flux_engine.concordance.record import UnsignedFallbackSigner

    packed = _packed(document, committed_rows)
    record = _record_for(compiled, committed, packed, UnsignedFallbackSigner(), committed_rows)
    decision = run_gate(
        compiled=compiled,
        record_document=record.document,
        committed=committed,
        packed=packed,
        now_ms=NOW_MS,
        trusted_keys=None,
    )
    _assert_only(decision, "record.signature")


def test_a_record_signed_by_another_key_withholds(scene) -> None:
    from flux_engine.concordance.record import Ed25519Signer

    other = Ed25519Signer(private_key=bytes(range(96, 128)), key_id="other")
    _assert_only(_run(scene, trusted_keys=other.public_key), "record.signature")


def test_a_malformed_record_withholds_before_anything_else(scene) -> None:
    decision = _run(scene, record_document={"recordVersion": 1})
    assert decision.action is GateAction.WITHHOLD
    assert decision.code == "FLX-8001"
    assert [check.name for check in decision.checks] == ["record.shape"]


def test_rows_that_do_not_digest_to_the_recorded_value_withhold(scene) -> None:
    """The rows in the pack are the ones digested, not the ones described."""
    packed = scene["packed"]
    swapped = PackedEmit(header=packed.header, rows=tuple(reversed(packed.rows)))
    _assert_only(_run(scene, packed=swapped), "synthetic.outputDigest")


def test_a_row_count_that_disagrees_with_the_record_withholds(scene) -> None:
    document = copy.deepcopy(dict(scene["record"].document))
    document["synthetic"] = {**document["synthetic"], "rows": 1}
    _assert_only(
        _run(scene, record_document=document), "synthetic.rowCount", also=_SIGNED_BODY_EDIT
    )


def test_a_record_naming_another_state_ordinal_withholds(scene) -> None:
    document = copy.deepcopy(dict(scene["record"].document))
    document["committedState"] = {**document["committedState"], "ordinal": 99}
    _assert_only(
        _run(scene, record_document=document), "committedState.ordinal", also=_SIGNED_BODY_EDIT
    )


def test_a_record_naming_another_commitment_withholds(scene) -> None:
    document = copy.deepcopy(dict(scene["record"].document))
    document["committedState"] = {**document["committedState"], "C2": "sha256:" + "0" * 64}
    _assert_only(_run(scene, record_document=document), "committedState.C2", also=_SIGNED_BODY_EDIT)


def test_rows_outside_a_margin_withhold(document, compiled, committed, signer) -> None:
    drifted = [
        {**row, "amount": str(6 + (index % 3))}
        for index, row in enumerate(document["committedSlice"])
    ]
    packed = _packed(document, drifted)
    record = _record_for(compiled, committed, packed, signer, drifted)
    decision = run_gate(
        compiled=compiled,
        record_document=record.document,
        committed=committed,
        packed=packed,
        now_ms=NOW_MS,
        trusted_keys=signer.public_key,
    )
    _assert_only(decision, "properties.outcomes")
    assert decision.evaluation is not None
    assert decision.evaluation.outside


def test_a_fail_closed_condition_at_ship_time_withholds(
    document, compiled, committed, signer
) -> None:
    """The record was written over clean rows; the pack carries dirty ones."""
    clean = document["committedSlice"]
    packed_clean = _packed(document, clean)
    record = _record_for(compiled, committed, packed_clean, signer, clean)
    dirty = [dict(row) for row in clean]
    dirty[0] = {**dirty[0], "amount": "999"}
    decision = run_gate(
        compiled=compiled,
        record_document=record.document,
        committed=committed,
        packed=_packed(document, dirty),
        now_ms=NOW_MS,
        trusted_keys=signer.public_key,
    )
    # A refusal condition that fires at ship time moves the emitted
    # stream, so the outcome property and the output digest move with it.
    _assert_only(
        decision,
        "properties.refusals",
        also=frozenset({"properties.outcomes", "synthetic.outputDigest"}),
    )
    assert decision.evaluation is not None
    assert decision.evaluation.refusals


def test_a_state_past_its_declared_maximum_age_withholds(scene, block) -> None:
    max_age = block.budget.max_state_age_ms
    assert max_age is not None
    fresh = _run(scene, now_ms=scene["committed"].committed_at_ms + max_age)
    assert fresh.action is GateAction.EMIT
    stale = _run(scene, now_ms=scene["committed"].committed_at_ms + max_age + 1)
    _assert_only(stale, "committedState.age")


def test_a_state_dated_after_the_gate_runs_withholds(scene) -> None:
    _assert_only(_run(scene, now_ms=scene["committed"].committed_at_ms - 1), "committedState.age")


def test_no_declared_maximum_age_leaves_the_age_check_passing(
    document, compiled, committed_rows, signer
) -> None:
    changed = copy.deepcopy(document)
    changed["extension"]["fidelity"]["budget"] = {"maxChecksPerState": 3}
    recompiled = compile_fidelity(
        contract=changed["contract"],
        emit=changed["emit"],
        extension=changed["extension"],
        expose=build_expose(changed),
    )
    from tests.concordance.conftest import build_committed

    state = build_committed(changed, recompiled)
    packed = _packed(changed, committed_rows)
    record = _record_for(recompiled, state, packed, signer, committed_rows)
    decision = run_gate(
        compiled=recompiled,
        record_document=record.document,
        committed=state,
        packed=packed,
        now_ms=state.committed_at_ms + 10**12,
        trusted_keys=signer.public_key,
    )
    assert decision.action is GateAction.EMIT
    age = next(check for check in decision.checks if check.name == "committedState.age")
    assert "no maximum age is declared" in age.detail


def test_the_gate_runs_every_check_even_after_one_fails(scene) -> None:
    document = dict(scene["record"].document)
    document["H"] = "sha256:" + "0" * 64
    decision = _run(scene, record_document=document)
    assert len(decision.checks) == 12
    assert len(decision.failures) >= 2  # the binding and the signature it covers


# ---------------------------------------------------------------------------
# The packed emit
# ---------------------------------------------------------------------------


def test_a_short_packed_row_is_refused(document) -> None:
    packed = PackedEmit(
        header={"columns": ["a", "b"]},
        rows=(("1",),),
    )
    with pytest.raises(FluxError) as caught:
        packed.as_mappings()
    assert caught.value.code.code == "FLX-8001"


def test_a_short_packed_row_withholds_rather_than_raising_out_of_the_gate(scene) -> None:
    packed = PackedEmit(header=scene["packed"].header, rows=(("1",),))
    decision = _run(scene, packed=packed)
    _assert_only(decision, "synthetic.rows")


def test_the_packed_rows_are_read_once_for_both_the_digest_and_the_statistics(
    scene,
) -> None:
    """One object, two views: there is no second dataset to disagree with."""
    packed = scene["packed"]
    mapped = packed.as_mappings()
    assert len(mapped) == len(packed.rows)
    assert list(mapped[0]) == list(packed.columns)
    for row, values in zip(mapped, packed.rows, strict=True):
        assert tuple(row[name] for name in packed.columns) == values


# ---------------------------------------------------------------------------
# The withheld CloudEvent
# ---------------------------------------------------------------------------


def test_a_withheld_decision_emits_the_cloud_event_the_spec_names(scene) -> None:
    document = dict(scene["record"].document)
    document["H"] = "sha256:" + "0" * 64
    decision = _run(scene, record_document=document)
    event = decision.withheld_event(
        event_id="01J0", time="2026-09-02T00:00:00.000Z", source="/flux/ship"
    )
    assert event["type"] == WITHHELD_EVENT_TYPE
    assert event["specversion"] == "1.0"
    assert event["id"] == "01J0"
    assert event["time"] == "2026-09-02T00:00:00.000Z"
    assert event["source"] == "/flux/ship"
    assert event["datacontenttype"] == "application/json"
    assert event["data"]["action"] == "withhold"
    assert event["data"]["code"] == "FLX-8001"


def test_the_withheld_event_names_every_failed_check(scene) -> None:
    document = dict(scene["record"].document)
    document["H"] = "sha256:" + "0" * 64
    decision = _run(scene, record_document=document)
    event = decision.withheld_event(event_id="1", time="2026-09-02T00:00:00.000Z", source="/s")
    failed = {check["check"] for check in event["data"]["checks"] if not check["passed"]}
    assert failed == {check.name for check in decision.failures}


def test_the_withheld_event_carries_no_record(scene, committed_rows) -> None:
    document = dict(scene["record"].document)
    document["H"] = "sha256:" + "0" * 64
    decision = _run(scene, record_document=document)
    rendered = json.dumps(
        decision.withheld_event(event_id="1", time="2026-09-02T00:00:00.000Z", source="/s"),
        sort_keys=True,
    )
    for row in committed_rows:
        assert row["subscriber_id"] not in rendered


def test_the_withheld_event_is_a_pure_function_of_its_arguments(scene) -> None:
    document = dict(scene["record"].document)
    document["H"] = "sha256:" + "0" * 64
    decision = _run(scene, record_document=document)
    args = {"event_id": "1", "time": "2026-09-02T00:00:00.000Z", "source": "/s"}
    assert decision.withheld_event(**args) == decision.withheld_event(**args)


def test_a_decision_to_emit_has_no_withheld_event(scene) -> None:
    with pytest.raises(FluxError) as caught:
        _run(scene).withheld_event(event_id="1", time="2026-09-02T00:00:00.000Z", source="/s")
    assert caught.value.code.code == "FLX-8001"


def test_the_withheld_event_serialises_as_ndjson(scene) -> None:
    """§7's default sink is stdout NDJSON, so the event has to be one line."""
    document = dict(scene["record"].document)
    document["H"] = "sha256:" + "0" * 64
    event = _run(scene, record_document=document).withheld_event(
        event_id="1", time="2026-09-02T00:00:00.000Z", source="/s"
    )
    line = json.dumps(event, sort_keys=True)
    assert "\n" not in line
    assert json.loads(line) == event
