# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""SEAM-FIDELITY-SPEC §1: which runtime attribute fills which declared column.

Every case here is about the *resolution*, not about a run: the module
under test takes a compiled expose, an alias table, an override block,
the set of source paths a run declares and the subset of them that is
always a whole number, and hands back one binding per column. That is
deliberate -- the mapping is decided at plan time, before a row exists, so
a contract the run cannot fill is refused once rather than on every row it
would have produced. KERNEL-SPEC §9's INT rule is the same kind of
decision, and is pinned at the end of this module.

Nothing here restates a source path the engine already declares. The
alias table under test is ``flux_engine.verticals.telco``'s own, the
column names come from a compiled expose, and the declared-source set is
built by :func:`flux_engine.seam.mapping.source_namespace`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Mapping

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.seam.conform import render_row
from flux_engine.seam.contract import compile_expose
from flux_engine.seam.emit import row_line
from flux_engine.seam.mapping import (
    BY_NAME_PREFIXES,
    INTEGER_SCALARS,
    ColumnMapping,
    integral_namespace,
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


# ---------------------------------------------------------------------------
# An INT column's source is decided at plan time (KERNEL-SPEC §9)
# ---------------------------------------------------------------------------

#: The towers count every tenant carries, and a towers amount beside it: the
#: count's every family draws whole numbers, the amount's draws cents.
COUNTS = ("tenancyCount",)
AMOUNTS = ("monthlyRecurringCharge",)


def _resolve_int(schema, *, aliases, overrides=None, whole=COUNTS) -> ColumnMapping:
    return resolve_column_mapping(
        _expose(schema),
        declared_sources=source_namespace(subject_attributes=COUNTS + AMOUNTS),
        integral_sources=integral_namespace(subject_attributes=whole),
        aliases=aliases,
        overrides=overrides,
    )


def _tenancy(column_type: str):
    return _resolve_int(
        [{"name": "tenancy_count", "type": column_type, "required": True}],
        aliases={"tenancy_count": "subject.tenancyCount"},
    )


def test_a_drawn_count_fills_an_int_column_and_renders_a_json_integer() -> None:
    """The headline case: a ``Decimal`` count reaches the stream as ``220``, not ``"220"``."""
    mapping = _tenancy("INTEGER")
    assert mapping.bindings[0].integral
    row = mapping.row({"subject.tenancyCount": Decimal("220")})
    assert row == (220,)
    assert type(row[0]) is int
    expose = _expose([{"name": "tenancy_count", "type": "INTEGER", "required": True}])
    rendered = render_row(row, expose)
    assert rendered == (220,)
    assert row_line(rendered) == b"[220]\n"


def test_a_source_that_can_hold_a_fraction_is_refused_for_an_int_column_at_plan_time() -> None:
    """``FLX-3303`` from the resolution itself: no row, no value, has been looked at."""
    with pytest.raises(FluxError) as error:
        _resolve_int(
            [{"name": "monthly_recurring_charge", "type": "INT", "required": True}],
            aliases={"monthly_recurring_charge": "subject.monthlyRecurringCharge"},
        )
    assert error.value.code.code == "FLX-3303"
    assert "monthly_recurring_charge" in str(error.value)
    assert "subject.monthlyRecurringCharge" in str(error.value)
    assert "alias" in str(error.value)


def test_an_override_onto_a_fractional_source_is_refused_by_the_same_rule() -> None:
    with pytest.raises(FluxError) as error:
        _resolve_int(
            [{"name": "tenancy_count", "type": "INT", "required": True}],
            aliases={"tenancy_count": "subject.tenancyCount"},
            overrides={"tenancy_count": "subject.monthlyRecurringCharge"},
        )
    assert error.value.code.code == "FLX-3303"
    assert "override" in str(error.value)


@pytest.mark.parametrize("source", ["subject.id", "subject.market", "event.type"])
def test_a_string_scalar_is_refused_for_an_int_column(source: str) -> None:
    """Refused at plan time, where before the first row's ``FLX-3003`` refused it."""
    with pytest.raises(FluxError) as error:
        _resolve(_expose([{"name": "n", "type": "INT"}]), aliases={"n": source})
    assert error.value.code.code == "FLX-3303"


@pytest.mark.parametrize("source", INTEGER_SCALARS)
def test_an_integer_scalar_fills_an_int_column_without_being_declared(source: str) -> None:
    """The engine carries these as ``int``, so no caller has to name them whole."""
    mapping = _resolve(_expose([{"name": "n", "type": "INT"}]), aliases={"n": source})
    assert mapping.bindings[0].integral
    assert mapping.row({source: 3}) == (3,)


def test_an_optional_int_column_with_no_source_renders_null() -> None:
    mapping = _resolve(_expose([{"name": "plan_code", "type": "INT"}]), aliases={})
    binding = mapping.bindings[0]
    assert (binding.source, binding.integral) == (None, False)
    assert mapping.row({}) == (None,)


def test_a_missing_value_in_an_int_column_is_left_to_the_seam() -> None:
    """``None`` is not converted: a required column turns it into ``FLX-3202``."""
    mapping = _tenancy("INT")
    assert mapping.row({}) == (None,)


def test_only_an_int_column_converts_a_decimal_0_column_keeps_its_decimal() -> None:
    """``DECIMAL(10,0)`` fed by the same count still renders its exact decimal string."""
    mapping = _tenancy("DECIMAL(10,0)")
    assert not mapping.bindings[0].integral
    row = mapping.row({"subject.tenancyCount": Decimal("220")})
    assert row == (Decimal("220"),)
    expose = _expose([{"name": "tenancy_count", "type": "DECIMAL(10,0)", "required": True}])
    assert render_row(row, expose) == ("220",)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("220"), 220),
        (Decimal("220.000"), 220),
        (Decimal("-0"), 0),
        (Decimal("1E+3"), 1000),
        (Decimal("-4"), -4),
    ],
)
def test_a_whole_decimal_becomes_exactly_the_int_it_equals(value: Decimal, expected: int) -> None:
    (cell,) = _tenancy("INT").row({"subject.tenancyCount": value})
    assert type(cell) is int
    assert cell == expected


@pytest.mark.parametrize("value", [Decimal("2.5"), Decimal("0.001"), Decimal("NaN")])
def test_a_fraction_from_a_source_the_plan_checked_is_refused_not_rounded(value: Decimal) -> None:
    """A broken promise, not data: ``FLX-3003``, and the value is never named."""
    with pytest.raises(FluxError) as error:
        _tenancy("INT").row({"subject.tenancyCount": value}, row_index=4)
    assert error.value.code.code == "FLX-3003"
    assert "row 4, column 'tenancy_count'" in str(error.value)
    assert "Decimal" in str(error.value)
    assert str(value) not in str(error.value)


@pytest.mark.parametrize("value", [True, "220"])
def test_a_value_that_is_not_a_decimal_is_left_to_the_renderer_s_class_rule(value: Any) -> None:
    """Only a ``Decimal`` is converted; the renderer judges every other class as before."""
    mapping = _tenancy("INT")
    row = mapping.row({"subject.tenancyCount": value})
    assert row == (value,)
    expose = _expose([{"name": "tenancy_count", "type": "INT", "required": True}])
    with pytest.raises(FluxError) as error:
        render_row(row, expose)
    assert error.value.code.code == "FLX-3003"


def test_the_renderer_alone_still_refuses_a_decimal_in_an_int_column() -> None:
    """The conversion is the plan's, not the renderer's: a row built without a mapping fails."""
    expose = _expose([{"name": "tenancy_count", "type": "INT", "required": True}])
    with pytest.raises(FluxError) as error:
        render_row((Decimal("220"),), expose)
    assert error.value.code.code == "FLX-3003"


def test_the_plan_document_says_which_columns_convert() -> None:
    document = _tenancy("INT").as_document()
    assert document["bindings"][0]["integral"] is True
    assert {entry["integral"] for entry in _resolve().as_document()["bindings"]} == {False}


def test_the_integer_scalars_are_declared_and_whole_whatever_the_vertical_adds() -> None:
    assert set(INTEGER_SCALARS) <= source_namespace()
    assert integral_namespace() == frozenset(INTEGER_SCALARS)
    assert "subject.traits.loyal" in integral_namespace(subject_traits=("loyal",))
    assert "event.data.attempts" in integral_namespace(event_payload_fields=("attempts",))
