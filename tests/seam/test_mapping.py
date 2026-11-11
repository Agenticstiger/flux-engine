# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""SEAM-FIDELITY-SPEC §1: which runtime attribute fills which declared column.

Every case here is about the *resolution*, not about a run: the module
under test takes a compiled expose, an alias table, an override block and
the set of source paths a run declares, and hands back one binding per
column. That is deliberate -- the mapping is decided at plan time, before
a row exists, so a contract the run cannot fill is refused once rather
than on every row it would have produced.

Nothing here restates a source path the engine already declares. The
alias table under test is ``flux_engine.verticals.telco``'s own, the
column names come from a compiled expose, and the declared-source set is
built by :func:`flux_engine.seam.mapping.source_namespace`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.seam.contract import compile_expose
from flux_engine.seam.mapping import (
    BY_NAME_PREFIXES,
    ColumnMapping,
    resolve_column_mapping,
    source_namespace,
)
from flux_engine.verticals import emit_aliases

pytestmark = pytest.mark.unit

#: The shipped ``payment-recovery.fluid.yml`` row, transcribed as a schema
#: rather than read from the bundle, so this module runs without the FLUX
#: checkout beside it. The integration test that reads the real contract
#: lives in ``tests/stages/test_scenario_emit.py``.
SCHEMA: List[Dict[str, Any]] = [
    {"name": "subscriber_id", "type": "STRING", "required": True},
    {"name": "aborted_at", "type": "TIMESTAMP", "required": True},
    {"name": "amount", "type": "DECIMAL(18,4)"},
    {"name": "market", "type": "STRING"},
]


def _expose(schema: List[Mapping[str, Any]], *, expose_id: str = "payment_recovery_moment"):
    return compile_expose(
        {
            "exposeId": expose_id,
            "kind": "stream",
            "contract": {"schema": [dict(column) for column in schema]},
            "binding": {"platform": "kafka", "format": "kafka_topic", "location": {"topic": "t"}},
        }
    )


def _declared(**extra: Any):
    """The source paths a telco run declares, plus whatever a case adds."""
    return source_namespace(
        subject_attributes=extra.get("subject_attributes", ("arpu", "tenureMonths")),
        subject_traits=extra.get("subject_traits", ("price_sensitivity",)),
        event_payload_fields=extra.get("event_payload_fields", ("amount", "attempts", "channel")),
    )


def _resolve(expose=None, *, overrides=None, aliases=None, declared=None) -> ColumnMapping:
    return resolve_column_mapping(
        expose if expose is not None else _expose(SCHEMA),
        declared_sources=declared if declared is not None else _declared(),
        aliases=emit_aliases("telco") if aliases is None else aliases,
        overrides=overrides,
    )


# ---------------------------------------------------------------------------
# The shipped row
# ---------------------------------------------------------------------------


def test_the_shipped_row_resolves_every_column_through_the_vertical() -> None:
    """The story's headline row: one binding per declared column."""
    mapping = _resolve()
    assert [binding.column for binding in mapping.bindings] == [column["name"] for column in SCHEMA]
    resolved = {binding.column: binding.source for binding in mapping.bindings}
    assert resolved == dict(emit_aliases("telco"))
    assert {binding.how for binding in mapping.bindings} == {"alias"}


def test_a_row_is_positional_and_aligned_to_the_declared_column_order() -> None:
    """KERNEL-SPEC §8 emits arrays aligned to the header, so order is meaning."""
    mapping = _resolve()
    values = {
        "subject.id": "sub-1",
        "event.time": 1_767_225_600_000,
        "event.data.amount": "12.5",
        "subject.market": "IE",
    }
    row = mapping.row(values)
    assert len(row) == len(SCHEMA)
    assert row == ("sub-1", 1_767_225_600_000, "12.5", "IE")


def test_a_source_the_run_does_not_hold_for_this_moment_renders_nothing() -> None:
    """A mapped path absent from one moment's values is ``None`` at the seam.

    :meth:`ColumnMapping.row` never invents: whether a ``None`` in a
    required column is a refusal is the emit writer's call (``FLX-3202``),
    made per row, not this module's.
    """
    assert _resolve().row({"subject.id": "sub-1"}) == ("sub-1", None, None, None)


# ---------------------------------------------------------------------------
# Three steps, in one order
# ---------------------------------------------------------------------------


def test_an_override_on_the_emit_beats_the_vertical_alias() -> None:
    mapping = _resolve(overrides={"aborted_at": "moment.firstTime"})
    binding = next(b for b in mapping.bindings if b.column == "aborted_at")
    assert (binding.source, binding.how) == ("moment.firstTime", "override")
    # every other column still comes from the alias table
    assert {b.how for b in mapping.bindings if b.column != "aborted_at"} == {"alias"}


def test_the_alias_beats_the_column_s_own_name() -> None:
    """``amount`` would resolve by name; the vertical's word decides instead."""
    amount_only = _expose([{"name": "amount", "type": "DECIMAL(18,4)"}])
    by_name = _resolve(amount_only, aliases={})
    assert by_name.bindings[0].source == "event.data.amount"
    assert by_name.bindings[0].how == "name"
    aliased = _resolve(amount_only, aliases={"amount": "subject.arpu"})
    assert (aliased.bindings[0].source, aliased.bindings[0].how) == ("subject.arpu", "alias")


@pytest.mark.parametrize("prefix", BY_NAME_PREFIXES)
def test_the_by_name_step_searches_each_namespace(prefix: str) -> None:
    schema = [{"name": "amount", "type": "DECIMAL(18,4)"}]
    declared = source_namespace(
        subject_attributes=("amount",) if prefix == "subject." else (),
        subject_traits=("amount",) if prefix == "subject.traits." else (),
        event_payload_fields=("amount",) if prefix == "event.data." else (),
    )
    mapping = _resolve(_expose(schema), aliases={}, declared=declared)
    assert mapping.bindings[0].source == f"{prefix}amount"
    assert mapping.bindings[0].how == "name"


def test_the_by_name_search_order_is_the_declared_one() -> None:
    """All three namespaces carry the name; the first in the fixed order wins."""
    schema = [{"name": "amount", "type": "DECIMAL(18,4)"}]
    declared = source_namespace(
        subject_attributes=("amount",),
        subject_traits=("amount",),
        event_payload_fields=("amount",),
    )
    mapping = _resolve(_expose(schema), aliases={}, declared=declared)
    assert mapping.bindings[0].source == f"{BY_NAME_PREFIXES[0]}amount"


# ---------------------------------------------------------------------------
# What is refused, and when
# ---------------------------------------------------------------------------


def test_a_required_column_no_step_resolves_is_refused_by_name() -> None:
    schema = SCHEMA + [{"name": "recovered_by", "type": "STRING", "required": True}]
    with pytest.raises(FluxError) as error:
        _resolve(_expose(schema))
    assert error.value.code.code == "FLX-3302"
    assert "recovered_by" in str(error.value)


def test_an_optional_column_no_step_resolves_renders_null() -> None:
    """The paired positive case: the refusal is about *required*, not about mapping."""
    schema = SCHEMA + [{"name": "recovered_by", "type": "STRING"}]
    mapping = _resolve(_expose(schema))
    binding = mapping.bindings[-1]
    assert (binding.source, binding.how, binding.required) == (None, "unmapped", False)
    assert mapping.row({"subject.id": "sub-1"})[-1] is None


def test_an_override_onto_an_attribute_the_run_does_not_declare_is_refused() -> None:
    with pytest.raises(FluxError) as error:
        _resolve(overrides={"amount": "event.data.refund"})
    assert error.value.code.code == "FLX-3302"
    assert "event.data.refund" in str(error.value)


def test_an_alias_onto_an_attribute_the_run_does_not_declare_is_refused() -> None:
    aliases = {**emit_aliases("telco"), "market": "subject.region"}
    with pytest.raises(FluxError) as error:
        _resolve(aliases=aliases)
    assert error.value.code.code == "FLX-3302"
    assert "subject.region" in str(error.value)


def test_an_override_for_a_column_the_expose_does_not_declare_is_refused() -> None:
    """An override that silently did nothing would be a declaration ignored."""
    with pytest.raises(FluxError) as error:
        _resolve(overrides={"churn_risk": "subject.arpu"})
    assert error.value.code.code == "FLX-3302"
    assert "churn_risk" in str(error.value)


@pytest.mark.parametrize("overrides", [[], "aborted_at=event.time", 7])
def test_a_mapping_block_that_is_not_a_mapping_is_refused(overrides: Any) -> None:
    with pytest.raises(FluxError) as error:
        _resolve(overrides=overrides)
    assert error.value.code.code == "FLX-3302"


def test_an_override_whose_source_is_not_a_path_string_is_refused() -> None:
    with pytest.raises(FluxError) as error:
        _resolve(overrides={"amount": ["event.data.amount"]})
    assert error.value.code.code == "FLX-3302"


# ---------------------------------------------------------------------------
# The mapping is a declaration a plan can carry
# ---------------------------------------------------------------------------


def test_the_mapping_renders_as_a_document_a_plan_can_hold() -> None:
    document = _resolve().as_document()
    assert document["exposeId"] == "payment_recovery_moment"
    assert [entry["column"] for entry in document["bindings"]] == [
        column["name"] for column in SCHEMA
    ]
    assert {entry["resolvedBy"] for entry in document["bindings"]} == {"alias"}


def test_resolution_is_a_pure_function_of_its_declarations() -> None:
    assert _resolve().as_document() == _resolve().as_document()


def test_the_closed_scalars_are_declared_whatever_the_vertical_adds() -> None:
    """A run with no vertical attributes still declares subject, event, moment."""
    empty = source_namespace()
    assert {"subject.id", "event.time", "moment.lastTime"} <= empty
    assert not any(path.startswith("subject.traits.") for path in empty)
