# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""KERNEL-SPEC §10: the flux-trace/1 vocabulary and the run digest document.

The op table in :data:`flux_engine.core.trace.OP_FIELDS` is the spec's
table transcribed, so the tests here do not restate it field by field --
that would only assert that a copy equals its original. What they pin is
the behaviour that table is supposed to buy: a closed vocabulary, an
exact field set, component digests that cannot collide, and a golden
failure that can name what moved.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from flux_engine.core.digest import (
    DigestDomain,
    digest_of_bytes,
    digest_of_document,
    digest_of_stream,
)
from flux_engine.core.errors import FluxError
from flux_engine.core.trace import (
    COMPONENT_OPS,
    EXTERNAL_COMPONENTS,
    GOLDEN_CHANGELOG_FILENAME,
    OP_FIELDS,
    RUN_FORMAT_VERSION,
    TRACE_VERSION,
    GoldenUpdate,
    RunComponents,
    TraceOp,
    build_run_digest_document,
    check_golden_update,
    component_digest,
    component_digests,
    component_of,
    golden_changelog_row,
    moved_components,
    require_golden_update,
    trace_record,
)

# --- the vocabulary -----------------------------------------------------


@pytest.mark.unit
def test_the_vocabulary_is_closed() -> None:
    assert TRACE_VERSION == "flux-trace/1"
    assert set(OP_FIELDS) == set(TraceOp)
    with pytest.raises(FluxError) as excinfo:
        trace_record("draw", stream="x")
    assert excinfo.value.code.code == "FLX-2103"


@pytest.mark.unit
def test_every_op_field_name_is_unique_within_its_op() -> None:
    for op, fields in OP_FIELDS.items():
        assert len(fields) == len(set(fields)), op
        assert "op" not in fields, f"{op}: 'op' is the discriminator, not a field"


@pytest.mark.unit
def test_a_record_carries_its_op_and_exactly_its_declared_fields() -> None:
    record = trace_record(TraceOp.MARKET, ordinal=7, market="mkt-a")
    assert record == {"op": "market", "ordinal": 7, "market": "mkt-a"}


@pytest.mark.unit
def test_a_missing_field_is_refused() -> None:
    with pytest.raises(FluxError) as excinfo:
        trace_record(TraceOp.MARKET, ordinal=7)
    assert excinfo.value.code.code == "FLX-2104"
    assert "market" in str(excinfo.value)


@pytest.mark.unit
def test_an_extra_field_is_refused() -> None:
    """The mechanism that catches a wall-clock field before it is hashed."""
    with pytest.raises(FluxError) as excinfo:
        trace_record(TraceOp.MARKET, ordinal=7, market="mkt-a", emittedAtMs=1)
    assert excinfo.value.code.code == "FLX-2104"
    assert "emittedAtMs" in str(excinfo.value)


@pytest.mark.unit
def test_a_field_value_with_no_canonical_spelling_is_refused_here() -> None:
    with pytest.raises(FluxError) as excinfo:
        trace_record(TraceOp.TRAIT, ordinal=1, name="digitalFluency", value=0.5)
    assert excinfo.value.code.code == "FLX-1001"


@pytest.mark.unit
def test_string_and_enum_spellings_of_an_op_agree() -> None:
    assert trace_record("market", ordinal=1, market="m") == trace_record(
        TraceOp.MARKET, ordinal=1, market="m"
    )


# --- components ---------------------------------------------------------


@pytest.mark.unit
def test_every_op_belongs_to_one_component_or_is_digested_elsewhere() -> None:
    """Totality: no op is silently outside every component digest."""
    grouped = {op for ops in COMPONENT_OPS.values() for op in ops}
    ungrouped = set(TraceOp) - grouped
    assert ungrouped == {TraceOp.EMIT, TraceOp.DECISION}
    assert EXTERNAL_COMPONENTS == ("emits", "decisions")
    for op in grouped:
        assert component_of(op) is not None
    for op in ungrouped:
        assert component_of(op) is None


@pytest.mark.unit
def test_no_op_appears_in_two_components() -> None:
    seen: List[TraceOp] = [op for ops in COMPONENT_OPS.values() for op in ops]
    assert len(seen) == len(set(seen))


@pytest.mark.unit
def test_two_empty_components_do_not_share_a_digest() -> None:
    """Why the digest is taken over an envelope naming the component."""
    digests = {name: component_digest(name, []).value for name in COMPONENT_OPS}
    assert len(set(digests.values())) == len(digests)


@pytest.mark.unit
def test_a_component_digest_moves_when_any_of_its_records_moves() -> None:
    first = [trace_record(TraceOp.MARKET, ordinal=0, market="mkt-a")]
    second = [trace_record(TraceOp.MARKET, ordinal=0, market="mkt-b")]
    assert component_digest("population", first) != component_digest("population", second)


@pytest.mark.unit
def test_a_component_digest_depends_on_record_order() -> None:
    """The scheduler's order is part of what a golden pins."""
    records = [
        trace_record(TraceOp.MARKET, ordinal=0, market="mkt-a"),
        trace_record(TraceOp.MARKET, ordinal=1, market="mkt-b"),
    ]
    assert component_digest("population", records) != component_digest(
        "population", list(reversed(records))
    )


@pytest.mark.unit
def test_a_record_from_another_component_is_refused() -> None:
    with pytest.raises(FluxError) as excinfo:
        component_digest(
            "population", [trace_record(TraceOp.ASSIGN, experimentId="e", ordinal=0, variant="a")]
        )
    assert excinfo.value.code.code == "FLX-2104"


@pytest.mark.unit
def test_an_unknown_component_is_refused() -> None:
    with pytest.raises(FluxError) as excinfo:
        component_digest("emits", [])
    assert excinfo.value.code.code == "FLX-2103"


def _trace() -> List[Dict[str, Any]]:
    return [
        trace_record(TraceOp.COHORT, worldId="w", lifecycle="active", count=3),
        trace_record(TraceOp.MARKET, ordinal=0, market="mkt-a"),
        trace_record(
            TraceOp.SIGNAL,
            t=0,
            signalId="s",
            ordinal=0,
            occurrence=0,
            payloadDigest="sha256:" + "0" * 64,
        ),
        trace_record(
            TraceOp.TRANSITION,
            t=1,
            journeyId="j",
            ordinal=0,
            fromState="a",
            toState="b",
            outcome="ok",
            success=True,
        ),
        trace_record(TraceOp.DELIVER, t=2, campaignId="c", ordinal=0, channelId="sms"),
        trace_record(TraceOp.ASSIGN, experimentId="e", ordinal=0, variant="control"),
        trace_record(TraceOp.EMIT, exposeId="x", rowIndex=0),
        trace_record(
            TraceOp.DECISION,
            seq=0,
            policyDigest="sha256:" + "0" * 64,
            reasonCode="allow",
            allow=True,
        ),
    ]


@pytest.mark.unit
def test_component_digests_partitions_a_whole_trace() -> None:
    digests = component_digests(_trace())
    assert set(digests) == set(COMPONENT_OPS)
    assert all(ref.domain is DigestDomain.JCS_DOCUMENT for ref in digests.values())


@pytest.mark.unit
def test_component_digests_ignores_the_externally_digested_ops() -> None:
    """Dropping the emit/decision ops must not move any component digest."""
    full = component_digests(_trace())
    without = component_digests(
        [record for record in _trace() if record["op"] not in {"emit", "decision"}]
    )
    assert full == without


@pytest.mark.unit
def test_component_digests_refuses_a_record_with_no_op() -> None:
    with pytest.raises(FluxError) as excinfo:
        component_digests([{"ordinal": 0}])
    assert excinfo.value.code.code == "FLX-2104"


# --- the run digest document -------------------------------------------


def _components(**overrides: Any) -> RunComponents:
    digests = component_digests(_trace())
    fields: Dict[str, Any] = dict(digests)
    fields["emits"] = {"payment_recovery_moment": digest_of_stream(b"rows", subject="emit")}
    fields["decisions"] = digest_of_bytes(b"{}\n", subject="decisions.ndjson")
    fields.update(overrides)
    return RunComponents(**fields)


def _run(**overrides: Any) -> Any:
    arguments: Dict[str, Any] = {
        "kernel_version": "0.1.0",
        "input_digest": digest_of_document({"in": 1}, subject="inputDigest"),
        "plan_digest": digest_of_document({"plan": 1}, subject="planDigest"),
        "components": _components(),
    }
    arguments.update(overrides)
    return build_run_digest_document(**arguments)


@pytest.mark.unit
def test_the_run_digest_document_has_the_spec_shape() -> None:
    document = _run().document
    assert document["runFormatVersion"] == RUN_FORMAT_VERSION
    assert document["traceVersion"] == TRACE_VERSION
    assert set(document) == {
        "runFormatVersion",
        "traceVersion",
        "kernelVersion",
        "inputDigest",
        "planDigest",
        "components",
    }
    assert set(document["components"]) == set(COMPONENT_OPS) | set(EXTERNAL_COMPONENTS)


@pytest.mark.unit
def test_the_run_digest_is_a_jcs_document_digest() -> None:
    built = _run()
    assert built.ref.domain is DigestDomain.JCS_DOCUMENT
    assert built.ref == digest_of_document(built.document, subject="runDigest")


@pytest.mark.unit
def test_an_emit_digest_from_the_wrong_domain_is_refused() -> None:
    """An outputDigest is the emit stream's, not a parsed document's."""
    wrong = _components(emits={"x": digest_of_document({"rows": []}, subject="x")})
    with pytest.raises(FluxError) as excinfo:
        _run(components=wrong)
    assert excinfo.value.code.code == "FLX-1002"


@pytest.mark.unit
def test_the_decisions_digest_must_be_over_raw_bytes() -> None:
    wrong = _components(decisions=digest_of_document({"d": 1}, subject="decisions"))
    with pytest.raises(FluxError) as excinfo:
        _run(components=wrong)
    assert excinfo.value.code.code == "FLX-1002"


@pytest.mark.unit
def test_the_run_digest_moves_when_a_component_moves() -> None:
    baseline = _run().ref
    moved = _run(
        components=_components(
            emits={"payment_recovery_moment": digest_of_stream(b"other rows", subject="emit")}
        )
    ).ref
    assert baseline != moved


# --- naming what moved --------------------------------------------------


@pytest.mark.unit
def test_moved_components_names_the_component_that_moved() -> None:
    before = _run().document["components"]
    after = _run(
        components=_components(decisions=digest_of_bytes(b"changed\n", subject="decisions.ndjson"))
    ).document["components"]
    assert moved_components(before, after) == ("decisions",)


@pytest.mark.unit
def test_moved_components_reaches_into_the_emits_map() -> None:
    before = _run().document["components"]
    after = _run(
        components=_components(
            emits={"payment_recovery_moment": digest_of_stream(b"other", subject="emit")}
        )
    ).document["components"]
    assert moved_components(before, after) == ("emits.payment_recovery_moment",)


@pytest.mark.unit
def test_an_emit_that_stopped_being_produced_has_moved() -> None:
    before = _run().document["components"]
    after = _run(components=_components(emits={})).document["components"]
    assert moved_components(before, after) == ("emits.payment_recovery_moment",)


@pytest.mark.unit
def test_an_identical_run_moves_nothing() -> None:
    assert moved_components(_run().document["components"], _run().document["components"]) == ()


# --- the GOLDEN-CHANGELOG discipline ------------------------------------


def _update(**overrides: Any) -> GoldenUpdate:
    fields: Dict[str, Any] = {
        "kernel_version": "0.2.0",
        "previous_digest": "sha256:" + "0" * 64,
        "new_digest": "sha256:" + "1" * 64,
        "reason": "trait draw order changed with the new seed path",
        "components": ("population",),
    }
    fields.update(overrides)
    return GoldenUpdate(**fields)


@pytest.mark.unit
def test_a_complete_update_with_its_row_is_admissible() -> None:
    update = _update()
    changelog = f"# Golden changelog\n\n{golden_changelog_row(update)}\n"
    assert check_golden_update(update, changelog) == ()
    require_golden_update(update, changelog)


@pytest.mark.unit
def test_the_row_names_the_components_that_moved() -> None:
    row = golden_changelog_row(_update(components=("population", "emits.x")))
    assert "population, emits.x" in row
    assert row.startswith("| 0.2.0 |")


@pytest.mark.unit
def test_a_row_survives_a_reason_containing_a_table_delimiter() -> None:
    row = golden_changelog_row(_update(reason="fixed a | b ordering\nacross two lines"))
    assert row.count("|") == 6
    assert "\n" not in row


@pytest.mark.unit
@pytest.mark.parametrize("reason", ["", "   ", "typo"])
def test_an_update_without_a_real_reason_is_refused(reason: str) -> None:
    update = _update(reason=reason)
    changelog = golden_changelog_row(update)
    problems = check_golden_update(update, changelog)
    assert problems
    with pytest.raises(FluxError) as excinfo:
        require_golden_update(update, changelog)
    assert excinfo.value.code.code == "FLX-2105"


@pytest.mark.unit
def test_an_update_with_no_changelog_row_is_refused() -> None:
    update = _update()
    problems = check_golden_update(update, "# Golden changelog\n")
    assert any(GOLDEN_CHANGELOG_FILENAME in problem for problem in problems)


@pytest.mark.unit
def test_a_baseline_that_did_not_move_is_refused() -> None:
    digest = "sha256:" + "2" * 64
    update = _update(previous_digest=digest, new_digest=digest)
    problems = check_golden_update(update, golden_changelog_row(update))
    assert any("nothing moved" in problem for problem in problems)


@pytest.mark.unit
def test_a_first_baseline_has_no_previous_digest() -> None:
    update = _update(previous_digest=None)
    assert check_golden_update(update, golden_changelog_row(update)) == ()
    assert "| - |" in golden_changelog_row(update)
